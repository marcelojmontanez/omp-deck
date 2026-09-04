import argparse
import json
import shutil
import subprocess
import signal
import socket
import sys
from pathlib import Path
from .auth import AuthManager, validate_auth_state

from .lifecycle import (
    backup_plugin,
    capture_service_state,
    check_health,
    discard_plugin_backup,
    install_plugin,
    install_service,
    plugin_installed,
    plugin_status,
    purge_installation,
    remove_plugin,
    remove_service,
    restart_service,
    restore_plugin,
    restore_service_state,
    run,
    service_status,
)
from .installation_paths import installation_paths
from .settings import config_path, load_settings


def _tailscale_status():
    result = subprocess.run(
        ["tailscale", "serve", "status", "--json"],
        capture_output=True,
        text=True,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(
            "tailscale serve status --json failed"
            + (f": {detail}" if detail else "")
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("tailscale serve status returned invalid JSON") from error
    if not isinstance(payload, dict):
        raise RuntimeError("tailscale serve status returned an invalid result")
    return payload


def _tailscale_proxies_to(payload, port):
    expected = f"http://127.0.0.1:{port}"
    web = payload.get("Web")
    if not isinstance(web, dict):
        return False
    return any(
        isinstance(site, dict)
        and isinstance(site.get("Handlers"), dict)
        and any(
            isinstance(handler, dict) and handler.get("Proxy") == expected
            for handler in site["Handlers"].values()
        )
        for site in web.values()
    )

def _tailscale_only_proxies_to(payload, port):
    expected = f"http://127.0.0.1:{port}"
    web = payload.get("Web")
    if payload.get("TCP") or not isinstance(web, dict) or not web:
        return False
    handlers = []
    for site in web.values():
        if not isinstance(site, dict) or not isinstance(site.get("Handlers"), dict):
            return False
        handlers.extend(site["Handlers"].values())
    return bool(handlers) and all(
        isinstance(handler, dict) and handler.get("Proxy") == expected
        for handler in handlers
    )

def _reset_tailscale():
    result = subprocess.run(
        ["tailscale", "serve", "reset"],
        capture_output=True,
        text=True,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(
            "tailscale serve reset failed" + (f": {detail}" if detail else "")
        )


def disable_tailscale(settings):
    status = _tailscale_status()
    if not _tailscale_only_proxies_to(status, settings.port):
        raise RuntimeError("refusing to remove an unrecognized Tailscale Serve route")
    _reset_tailscale()


def enable_tailscale(settings):
    if shutil.which("tailscale") is None:
        raise RuntimeError("Tailscale is not installed")
    existing = _tailscale_status()
    if existing.get("Web") or existing.get("TCP"):
        raise RuntimeError("existing Tailscale Serve routes were left unchanged")
    result = subprocess.run(
        ["tailscale", "serve", "--bg", "--yes", str(settings.port)],
        capture_output=True,
        text=True,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(
            f"tailscale serve --bg --yes {settings.port} failed"
            + (f": {detail}" if detail else "")
        )
    try:
        if not _tailscale_proxies_to(_tailscale_status(), settings.port):
            raise RuntimeError("Tailscale Serve did not expose the OMP Deck listener")
    except Exception as error:
        try:
            _reset_tailscale()
        except Exception as cleanup_error:
            raise RuntimeError(
                f"{error}; Tailscale rollback failed: {cleanup_error}"
            ) from error
        raise
    return True


def print_pairing(secret):
    print(f"Pairing code: {secret.token}")
    print(f"Expires at Unix time {secret.expires_at}.")


def pair():
    settings = load_settings()
    print_pairing(AuthManager(settings.state_root).begin_pairing())


def setup(*, tailscale=False):
    path = config_path()
    settings = load_settings(path)
    if sys.platform != "linux" or shutil.which("systemctl") is None:
        raise RuntimeError("automatic service setup requires Linux with systemd")
    if shutil.which(settings.omp_bin) is None and not Path(settings.omp_bin).is_file():
        raise RuntimeError("stock OMP is not installed or not on PATH")
    plugin_preexisting = plugin_installed(settings)
    service_snapshot = capture_service_state()
    manager = AuthManager(settings.state_root)
    plugin_backup = None
    plugin_touched = False
    service_touched = False
    tailscale_touched = False
    try:
        service_touched = True
        install_service(settings, path)
        check_health(settings)
        plugin_backup = backup_plugin(settings)
        if plugin_preexisting and plugin_backup is None:
            raise RuntimeError("cannot snapshot the installed OMP Deck plugin")
        plugin_touched = True
        install_plugin(settings)
        check_health(settings)
        if tailscale:
            tailscale_touched = enable_tailscale(settings)
        secret = manager.begin_pairing()
    except Exception as error:
        rollback_errors = []
        if tailscale_touched:
            try:
                disable_tailscale(settings)
            except Exception as cleanup_error:
                rollback_errors.append(f"Tailscale: {cleanup_error}")
        if plugin_touched:
            try:
                restore_plugin(settings, plugin_backup)
            except Exception as cleanup_error:
                rollback_errors.append(f"plugin: {cleanup_error}")
        if service_touched:
            try:
                restore_service_state(service_snapshot)
            except Exception as cleanup_error:
                rollback_errors.append(f"service: {cleanup_error}")
        if rollback_errors:
            raise RuntimeError(
                f"{error}; setup rollback failed: {'; '.join(rollback_errors)}"
            ) from error
        raise
    discard_plugin_backup(plugin_backup)
    print_pairing(secret)


def _check(label, healthy, *, optional=False, detail=""):
    return label, healthy, optional, detail


def _health_diagnostic(settings):
    try:
        check_health(settings, timeout=2)
    except RuntimeError as error:
        try:
            with socket.socket() as probe:
                probe.bind((settings.host, settings.port))
        except OSError:
            listener = "listener occupied but /healthz is unavailable"
        else:
            listener = "listener is not running"
        return False, f"{listener}; {error}"
    return True, f"http://{settings.host}:{settings.port}/healthz"


def doctor():
    checks = []
    try:
        settings = load_settings()
    except RuntimeError as error:
        settings = None
        checks.append(_check("Configuration", False, detail=str(error)))
    else:
        checks.append(_check("Configuration", True, detail=str(config_path())))

    if settings is None:
        for label in ("Service", "Health", "OMP executable", "Plugin compatibility", "Authentication"):
            checks.append(_check(label, False, detail="blocked by invalid configuration"))
    else:
        health, health_detail = _health_diagnostic(settings)
        omp_available = shutil.which(settings.omp_bin) is not None or Path(settings.omp_bin).is_file()
        checks.extend(
            (
                _check(
                    "Service",
                    service_status() == 0,
                    detail="systemd user service status",
                ),
                _check("Health", health, detail=health_detail),
                _check(
                    "OMP executable",
                    omp_available,
                    detail=settings.omp_bin,
                ),
                _check(
                    "Plugin compatibility",
                    plugin_status(settings) if omp_available else False,
                    detail="expected omp-deck 0.1.0 for OMP 18.1.10",
                ),
                _check(
                    "Authentication",
                    _auth_healthy(settings),
                    detail=str(settings.state_root / "auth.json"),
                ),
            )
        )
    tailscale = shutil.which("tailscale")
    checks.append(
        _check(
            "Tailscale CLI",
            tailscale is not None,
            optional=True,
            detail=tailscale or "optional local-only dependency is absent",
        )
    )
    for label, healthy, optional, detail in checks:
        state = "OK" if healthy else ("OPTIONAL" if optional else "FAIL")
        suffix = f" — {detail}" if detail else ""
        print(f"{state:8} {label}{suffix}")
    return 0 if all(healthy or optional for _, healthy, optional, _ in checks) else 1


def _auth_healthy(settings):
    try:
        validate_auth_state(settings.state_root)
    except RuntimeError:
        return False
    return True

def validate_config():
    path = config_path()
    settings = load_settings(path)
    print(f"OK       Configuration: {path}")
    print(f"OK       Listener: {settings.host}:{settings.port}")
    print(f"OK       Project root: {settings.project_root}")
    return 0


def serve():
    from .server import HOST, PORT, Handler, ThreadingHTTPServer, start_runtime, stop_runtime

    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def terminate(_signum, _frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, terminate)
    try:
        start_runtime()
        print(f"OMP Deck ready at http://{HOST}:{PORT}", flush=True)
        httpd.serve_forever()
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
        httpd.server_close()
        stop_runtime()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="omp-deck")
    commands = parser.add_subparsers(dest="command", required=True)
    setup_parser = commands.add_parser("setup", help="configure plugin and service")
    setup_parser.add_argument(
        "--tailscale",
        action="store_true",
        help="configure Tailscale Serve only when no routes already exist",
    )
    commands.add_parser("pair", help="create a one-time device pairing code")
    config = commands.add_parser("config", help="validate configuration")
    config_actions = config.add_subparsers(dest="config_action", required=True)
    config_actions.add_parser("validate")
    commands.add_parser("doctor", help="check configuration and integrations")
    commands.add_parser("serve", help="run OMP Deck in the foreground")
    commands.add_parser("purge", help="remove service, plugin, configuration, and state")
    migrate_parser = commands.add_parser(
        "migrate", help="plan or apply legacy session and cursor migration"
    )
    migrate_parser.add_argument(
        "--apply",
        action="store_true",
        help="write normalized registry and cursor records",
    )

    service = commands.add_parser("service", help="manage the systemd user service")
    service_actions = service.add_subparsers(dest="service_action", required=True)
    service_actions.add_parser(
        "install", help="generate, enable, and start the service"
    )
    for action in ("status", "restart", "remove"):
        service_actions.add_parser(action)

    plugin = commands.add_parser("plugin", help="manage the OMP plugin")
    plugin_actions = plugin.add_subparsers(dest="plugin_action", required=True)
    plugin_actions.add_parser("install")
    plugin_actions.add_parser("remove")

    arguments = parser.parse_args(argv)
    if arguments.command == "setup":
        setup(tailscale=arguments.tailscale)
    elif arguments.command == "pair":
        pair()
    elif arguments.command == "config":
        return validate_config()
    elif arguments.command == "doctor":
        return doctor()
    elif arguments.command == "serve":
        serve()
    elif arguments.command == "purge":
        purge_installation(load_settings().omp_bin)
    elif arguments.command == "migrate":
        from .migration import apply_migration, migration_plan

        settings = load_settings()
        result = apply_migration(settings) if arguments.apply else migration_plan(settings)
        print(json.dumps(result, sort_keys=True))
    elif arguments.command == "service":
        if arguments.service_action == "install":
            settings = load_settings()
            install_service(settings, config_path())
        elif arguments.service_action == "status":
            return service_status()
        elif arguments.service_action == "restart":
            restart_service()
        else:
            remove_service()
    elif arguments.plugin_action == "install":
        install_plugin(load_settings())
    else:
        try:
            omp_bin = load_settings().omp_bin
        except RuntimeError:
            omp_bin = "omp"
        remove_plugin(omp_bin)
    return 0


def entrypoint():
    try:
        return main()
    except (OSError, RuntimeError, subprocess.CalledProcessError, ValueError) as error:
        print(f"omp-deck: {error}", file=sys.stderr)
        return 1
