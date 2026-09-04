import json
import os
import subprocess
import shutil
from dataclasses import dataclass
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from .installation_paths import (
    MANIFEST_NAME,
    OWNERSHIP_MARKER,
    has_ownership_marker,
    installation_paths,
    manifest_content,
    mark_owned,
    remove_owned_root,
    validate_manifest,
)

PLUGIN_VERSION = "0.1.0"
PLUGIN_NAME = "omp-deck"
PLUGIN_ROOT = Path(__file__).resolve().parent / "plugin"

@dataclass(frozen=True)
class ServiceSnapshot:
    unit: bytes | None
    unit_mode: int | None
    manifest: bytes | None
    manifest_mode: int | None
    enabled: bool
    active: bool


def _file_snapshot(path):
    if not path.is_file() or path.is_symlink():
        return None, None
    return path.read_bytes(), path.stat().st_mode & 0o777


def capture_service_state(*, runner=None):
    runner = runner or run
    paths = installation_paths()
    unit, unit_mode = _file_snapshot(paths.unit_path)
    manifest, manifest_mode = _file_snapshot(paths.manifest_path)
    enabled = runner(
        ["systemctl", "--user", "is-enabled", "omp-deck.service"],
        check=False,
        capture_output=True,
    ).returncode == 0
    active = runner(
        ["systemctl", "--user", "is-active", "omp-deck.service"],
        check=False,
        capture_output=True,
    ).returncode == 0
    return ServiceSnapshot(unit, unit_mode, manifest, manifest_mode, enabled, active)


def restore_service_state(snapshot, *, runner=None):
    runner = runner or run
    paths = installation_paths()
    if snapshot.unit is None:
        runner(
            ["systemctl", "--user", "disable", "--now", "omp-deck.service"],
            check=False,
        )
    for path, content, mode in (
        (paths.unit_path, snapshot.unit, snapshot.unit_mode),
        (paths.manifest_path, snapshot.manifest, snapshot.manifest_mode),
    ):
        if content is None:
            path.unlink(missing_ok=True)
        else:
            write_atomic(path, content.decode("utf-8"), mode)
    runner(["systemctl", "--user", "daemon-reload"])
    if snapshot.unit is None:
        return
    runner(
        [
            "systemctl",
            "--user",
            "enable" if snapshot.enabled else "disable",
            "omp-deck.service",
        ]
    )
    runner(
        [
            "systemctl",
            "--user",
            "start" if snapshot.active else "stop",
            "omp-deck.service",
        ]
    )


def _plugin_record(settings, *, runner=None):
    runner = runner or run
    result = runner(
        [settings.omp_bin, "plugin", "list", "--json"],
        check=False,
        capture_output=True,
    )
    if result.returncode:
        return None
    try:
        records = json.loads(result.stdout).get("npm", [])
        return next(
            item
            for item in records
            if isinstance(item, dict) and item.get("name") == PLUGIN_NAME
        )
    except (AttributeError, StopIteration, TypeError, json.JSONDecodeError):
        return None


def plugin_installed(settings, *, runner=None):
    return _plugin_record(settings, runner=runner) is not None


def backup_plugin(settings, *, runner=None):
    record = _plugin_record(settings, runner=runner)
    try:
        source = Path(record["path"]).resolve(strict=True)
    except (KeyError, TypeError, ValueError, OSError):
        return None
    root = Path(settings.state_root) / "setup-rollback"
    backup = root / "plugin"
    root.mkdir(parents=True, exist_ok=True)
    if source == backup.resolve(strict=False):
        return backup
    if backup.exists():
        shutil.rmtree(backup)
    shutil.copytree(source, backup, ignore=shutil.ignore_patterns("node_modules"))
    return backup


def restore_plugin(settings, backup, *, runner=None):
    runner = runner or run
    if backup is None:
        return remove_plugin(settings.omp_bin, runner=runner)
    return runner([settings.omp_bin, "plugin", "install", str(backup)])


def discard_plugin_backup(backup):
    if backup is None:
        return
    root = backup.parent
    shutil.rmtree(backup, ignore_errors=True)
    try:
        root.rmdir()
    except OSError:
        pass


def run(command, *, check=True, capture_output=False):
    return subprocess.run(
        command,
        check=check,
        capture_output=capture_output,
        text=True,
    )


def write_atomic(path, content, mode=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if mode is not None:
            temporary.chmod(mode)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def systemd_quote(value):
    value = str(value)
    if "\0" in value or "\n" in value or "\r" in value:
        raise ValueError("systemd values cannot contain control characters")
    return '"' + value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"') + '"'


def service_unit(config_path, executable=sys.executable):
    return "\n".join(
        (
            "[Unit]",
            "Description=OMP Deck",
            "After=default.target",
            "",
            "[Service]",
            "Type=simple",
            f"Environment={systemd_quote(f'OMP_DECK_CONFIG={config_path}')}",
            f"ExecStart={systemd_quote(executable)} -m omp_deck serve",
            "Restart=on-failure",
            "RestartSec=2",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        )
    )


def prepare_installation(settings, config_path):
    paths = installation_paths(config_file=config_path, state_root=settings.state_root)
    mark_owned(paths.install_root)
    mark_owned(paths.config_root, allowed_existing=(paths.config_path,))
    mark_owned(
        paths.state_root,
        allowed_existing=(paths.state_root / "auth.json", paths.state_root / ".auth.lock"),
    )
    if not paths.config_path.exists():
        write_atomic(paths.config_path, "{}\n", 0o600)
    write_atomic(paths.manifest_path, manifest_content(paths), 0o600)
    return paths


def install_plugin(settings, *, runner=run):
    if not (PLUGIN_ROOT / "package.json").is_file():
        raise RuntimeError("bundled OMP Deck plugin is missing")
    runner([settings.omp_bin, "plugin", "install", str(PLUGIN_ROOT)])
    doctor = runner(
        [settings.omp_bin, "plugin", "doctor", "--json"],
        check=False,
        capture_output=True,
    )
    try:
        checks = json.loads(doctor.stdout)
    except (TypeError, json.JSONDecodeError):
        checks = None
    plugin_check = (
        next(
            (
                item
                for item in checks
                if isinstance(item, dict) and item.get("name") == f"plugin:{PLUGIN_NAME}"
            ),
            None,
        )
        if isinstance(checks, list)
        else None
    )
    if (
        not isinstance(checks, list)
        or not isinstance(plugin_check, dict)
        or plugin_check.get("status") != "ok"
        or not plugin_status(settings, runner=runner)
    ):
        raise RuntimeError("OMP Deck plugin validation failed")


def remove_plugin(omp_bin="omp", *, runner=run):
    return runner([omp_bin, "plugin", "uninstall", PLUGIN_NAME])


def install_service(settings, config_path, *, runner=run, start=True):
    base_paths = installation_paths()
    if base_paths.unit_path.exists():
        validate_manifest(base_paths)
    paths = prepare_installation(settings, config_path)
    write_atomic(paths.unit_path, service_unit(paths.config_path), 0o644)
    runner(["systemctl", "--user", "daemon-reload"])
    if start:
        runner(["systemctl", "--user", "enable", "--now", "omp-deck.service"])
    else:
        runner(["systemctl", "--user", "enable", "omp-deck.service"])
    return paths


def service_status(*, runner=run):
    return runner(
        ["systemctl", "--user", "status", "omp-deck.service"],
        check=False,
    ).returncode


def restart_service(*, runner=run):
    runner(["systemctl", "--user", "restart", "omp-deck.service"])


def remove_service(*, runner=run):
    paths = validate_manifest(installation_paths())
    runner(
        ["systemctl", "--user", "disable", "--now", "omp-deck.service"],
        check=False,
    )
    paths.unit_path.unlink(missing_ok=True)
    runner(["systemctl", "--user", "daemon-reload"], check=False)
    return paths


def purge_installation(omp_bin="omp", *, runner=run):
    paths = remove_service(runner=runner)
    remove_plugin(omp_bin, runner=runner)
    approved = (paths.config_root, paths.state_root)
    remove_owned_root(paths.config_root, approved)
    if paths.state_root != paths.config_root:
        remove_owned_root(paths.state_root, approved)
    remove_owned_root(paths.install_root, (paths.install_root,))


def check_health(settings, *, timeout=5.0):
    url = f"http://{settings.host}:{settings.port}/healthz"
    request = urllib.request.Request(url, headers={"Host": f"{settings.host}:{settings.port}"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read())
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as error:
        raise RuntimeError(f"OMP Deck health check failed: {error}") from error
    required = {"ok", "configuration", "authentication"}
    if (
        response.status != 200
        or not isinstance(payload, dict)
        or not required.issubset(payload)
        or not all(payload[key] is True for key in required)
    ):
        raise RuntimeError("OMP Deck health check reported an unhealthy service")
    return payload


def plugin_status(settings, *, runner=run):
    record = _plugin_record(settings, runner=runner)
    return bool(
        record
        and record.get("version") == PLUGIN_VERSION
        and (record.get("manifest") or {}).get("version") == PLUGIN_VERSION
    )
