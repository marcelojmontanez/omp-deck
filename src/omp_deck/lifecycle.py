import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from ._linux_service import service_unit
from ._plugin_inventory import plugin_record
from .installation_paths import (
    InstallationPaths,
    has_ownership_marker,
    installation_paths,
    manifest_content,
    mark_owned,
    remove_owned_root,
    validate_manifest,
    validate_owned_root,
    validate_ownership_claim,
)

PLUGIN_VERSION = "1.3.3"
PLUGIN_NAME = "omp-deck"
PLUGIN_ROOT = Path(__file__).resolve().parent / "plugin"


@dataclass(frozen=True)
class RootSnapshot:
    path: Path
    existed: bool
    mode: int | None
    marker_existed: bool
    entries: tuple[str, ...] = ()


@dataclass(frozen=True)
class ServiceSnapshot:
    unit: bytes | None
    unit_mode: int | None
    manifest: bytes | None
    manifest_mode: int | None
    enabled: bool
    active: bool
    installation: InstallationPaths | None = None
    roots: tuple[RootSnapshot, ...] = ()
    config: bytes | None = None
    config_mode: int | None = None


def _service_adapter():
    if os.name == "nt":
        from . import _windows_lifecycle

        return _windows_lifecycle
    if sys.platform == "darwin":
        from . import _macos_lifecycle

        return _macos_lifecycle
    return None


def _file_snapshot(path):
    if not path.is_file() or path.is_symlink():
        return None, None
    return path.read_bytes(), path.stat().st_mode & 0o777


def _required_file_snapshot(path):
    content, mode = _file_snapshot(path)
    if (path.exists() or path.is_symlink()) and content is None:
        raise RuntimeError(f"cannot snapshot unsafe installation file: {path}")
    return content, mode


def capture_service_state(*, settings=None, config_path=None, runner=None):
    adapter = _service_adapter()
    if adapter is not None:
        return adapter.capture_service_state(
            settings=settings,
            config_path=config_path,
            runner=runner or run,
        )
    runner = runner or run
    paths = (
        installation_paths(config_file=config_path, state_root=settings.state_root)
        if settings is not None and config_path is not None
        else installation_paths()
    )
    unit, unit_mode = _required_file_snapshot(paths.unit_path)
    manifest, manifest_mode = _required_file_snapshot(paths.manifest_path)
    config, config_mode = _required_file_snapshot(paths.config_path)
    enabled = (
        runner(
            ["systemctl", "--user", "is-enabled", "omp-deck.service"],
            check=False,
            capture_output=True,
        ).returncode
        == 0
    )
    active = (
        runner(
            ["systemctl", "--user", "is-active", "omp-deck.service"],
            check=False,
            capture_output=True,
        ).returncode
        == 0
    )
    roots = _capture_roots(paths)
    return ServiceSnapshot(
        unit,
        unit_mode,
        manifest,
        manifest_mode,
        enabled,
        active,
        paths,
        roots,
        config,
        config_mode,
    )


def _capture_roots(paths):
    roots = []
    for root in dict.fromkeys((paths.install_root, paths.config_root, paths.state_root)):
        existed = root.exists() and not root.is_symlink()
        entries = tuple(sorted(path.name for path in root.iterdir())) if existed else ()
        roots.append(
            RootSnapshot(
                root,
                existed,
                root.stat().st_mode & 0o777 if existed else None,
                has_ownership_marker(root) if existed else False,
                entries,
            )
        )
    return tuple(roots)


def _restore_root(snapshot):
    if not snapshot.existed:
        remove_owned_root(snapshot.path, (snapshot.path,))
        return
    if (
        not snapshot.marker_existed
        and snapshot.path.is_dir()
        and not snapshot.path.is_symlink()
        and has_ownership_marker(snapshot.path)
    ):
        preserved = set(snapshot.entries)
        for child in snapshot.path.iterdir():
            if child.name in preserved:
                continue
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink(missing_ok=True)
    if snapshot.mode is not None:
        snapshot.path.chmod(snapshot.mode)


def restore_service_state(snapshot, *, runner=None):
    adapter = _service_adapter()
    if adapter is not None:
        return adapter.restore_service_state(snapshot, runner=runner or run)
    runner = runner or run
    paths = snapshot.installation or installation_paths()
    runner(
        ["systemctl", "--user", "stop", "omp-deck.service"],
        check=False,
    )
    _restore_service_files(snapshot, paths)
    if snapshot.installation is not None:
        for root in reversed(snapshot.roots):
            _restore_root(root)
    runner(["systemctl", "--user", "daemon-reload"])
    if snapshot.unit is None:
        runner(
            ["systemctl", "--user", "disable", "omp-deck.service"],
            check=False,
        )
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
            "restart" if snapshot.active else "stop",
            "omp-deck.service",
        ]
    )


def _restore_service_files(snapshot, paths):
    for path, content, mode in (
        (paths.unit_path, snapshot.unit, snapshot.unit_mode),
        (paths.manifest_path, snapshot.manifest, snapshot.manifest_mode),
        (paths.config_path, snapshot.config, snapshot.config_mode),
    ):
        if content is None:
            path.unlink(missing_ok=True)
        else:
            write_atomic(path, content, mode)


def plugin_installed(settings, *, runner=None):
    return plugin_record(settings.omp_bin, PLUGIN_NAME, runner=runner or run) is not None


def backup_plugin(settings, *, runner=None):
    record = plugin_record(settings.omp_bin, PLUGIN_NAME, runner=runner or run)
    try:
        source = Path(record["path"]).resolve(strict=True)
    except (KeyError, TypeError, ValueError, OSError):
        return None
    state_root = Path(settings.state_root)
    prefix = f".{state_root.name}-setup-rollback-"
    if source.name == "plugin" and source.parent.name.startswith(prefix):
        return source
    root = Path(tempfile.mkdtemp(prefix=prefix, dir=state_root.parent))
    backup = root / "plugin"
    try:
        shutil.copytree(
            source,
            backup,
            ignore=shutil.ignore_patterns("node_modules"),
        )
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        raise
    return backup


def restore_plugin(settings, backup, *, runner=None):
    runner = runner or run
    if backup is None:
        if plugin_installed(settings, runner=runner):
            return remove_plugin(settings.omp_bin, runner=runner)
        return None
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
        if isinstance(content, bytes):
            stream = os.fdopen(fd, "wb")
        else:
            stream = os.fdopen(fd, "w", encoding="utf-8")
        with stream:
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


def prepare_installation(settings, config_path):
    if os.name == "nt":
        from ._windows_lifecycle import prepare_installation as prepare_windows_installation

        return prepare_windows_installation(settings, config_path)
    paths = installation_paths(config_file=config_path, state_root=settings.state_root)
    allowed_config = (paths.config_path,)
    allowed_state = (paths.state_root / "auth.json", paths.state_root / ".auth.lock")
    validate_ownership_claim(paths.install_root)
    validate_ownership_claim(paths.config_root, allowed_config)
    validate_ownership_claim(paths.state_root, allowed_state)
    mark_owned(paths.install_root)
    mark_owned(paths.config_root, allowed_existing=allowed_config, mode=0o700)
    mark_owned(paths.state_root, allowed_existing=allowed_state, mode=0o700)
    if not paths.config_path.exists():
        write_atomic(paths.config_path, "{}\n", 0o600)
    else:
        paths.config_path.chmod(0o600)
    write_atomic(paths.manifest_path, manifest_content(paths), 0o600)
    return paths


def preflight_service_install(settings, config_path):
    base_paths = installation_paths()
    if base_paths.unit_path.exists() or base_paths.unit_path.is_symlink():
        validate_manifest(base_paths)
        _required_file_snapshot(base_paths.unit_path)
    paths = installation_paths(config_file=config_path, state_root=settings.state_root)
    validate_ownership_claim(paths.install_root)
    validate_ownership_claim(paths.config_root, (paths.config_path,))
    if os.name != "nt":
        validate_ownership_claim(
            paths.state_root,
            (paths.state_root / "auth.json", paths.state_root / ".auth.lock"),
        )
    return paths


def install_plugin(settings, *, runner=run):
    if os.name == "nt":
        from ._private_state import ensure_private_directory

        ensure_private_directory(settings.socket_root)
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
            (item for item in checks if isinstance(item, dict) and item.get("name") == f"plugin:{PLUGIN_NAME}"),
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
    if plugin_record(omp_bin, PLUGIN_NAME, runner=runner) is None:
        return None
    return runner([omp_bin, "plugin", "uninstall", PLUGIN_NAME])


def install_service(settings, config_path, *, runner=run, start=True):
    adapter = _service_adapter()
    if adapter is not None:
        return adapter.install_service(
            settings,
            config_path,
            runner=runner,
            start=start,
        )
    preflight_service_install(settings, config_path)
    paths = prepare_installation(settings, config_path)
    write_atomic(paths.unit_path, service_unit(paths.config_path), 0o644)
    runner(["systemctl", "--user", "daemon-reload"])
    runner(["systemctl", "--user", "enable", "omp-deck.service"])
    if start:
        runner(["systemctl", "--user", "restart", "omp-deck.service"])
    return paths


def service_status(*, runner=run):
    adapter = _service_adapter()
    if adapter is not None:
        return adapter.service_status(runner=runner)
    return runner(
        ["systemctl", "--user", "status", "omp-deck.service"],
        check=False,
    ).returncode


def restart_service(*, runner=run):
    adapter = _service_adapter()
    if adapter is not None:
        return adapter.restart_service(runner=runner)
    runner(["systemctl", "--user", "restart", "omp-deck.service"])


def remove_service(*, runner=run):
    adapter = _service_adapter()
    if adapter is not None:
        return adapter.remove_service(runner=runner)
    paths = validate_manifest(installation_paths())
    _required_file_snapshot(paths.unit_path)
    result = runner(
        ["systemctl", "--user", "disable", "--now", "omp-deck.service"],
        check=False,
        capture_output=True,
    )
    if result.returncode:
        raise RuntimeError("cannot stop and disable the OMP Deck service")
    paths.unit_path.unlink(missing_ok=True)
    runner(["systemctl", "--user", "daemon-reload"])
    return paths


def purge_installation(omp_bin="omp", *, runner=run):
    paths = validate_manifest(installation_paths())
    approved = (paths.config_root, paths.state_root)
    validate_owned_root(paths.config_root, approved)
    _required_file_snapshot(paths.unit_path)
    if paths.state_root != paths.config_root:
        validate_owned_root(paths.state_root, approved)
    validate_owned_root(paths.install_root, (paths.install_root,))
    plugin_present = plugin_record(omp_bin, PLUGIN_NAME, runner=runner) is not None
    paths = remove_service(runner=runner)
    if plugin_present:
        runner([omp_bin, "plugin", "uninstall", PLUGIN_NAME])
    remove_owned_root(paths.config_root, approved)
    if paths.state_root != paths.config_root:
        remove_owned_root(paths.state_root, approved)
    remove_owned_root(paths.install_root, (paths.install_root,))


def check_health(settings, *, timeout=5.0):
    if timeout <= 0:
        raise ValueError("health timeout must be positive")
    url = f"http://{settings.host}:{settings.port}/healthz"
    request = urllib.request.Request(url, headers={"Host": f"{settings.host}:{settings.port}"})
    deadline = time.monotonic() + timeout
    last_error = None
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            with urllib.request.urlopen(request, timeout=min(1.0, remaining)) as response:
                payload = json.loads(response.read())
            required = {"ok", "configuration", "authentication"}
            if (
                response.status == 200
                and isinstance(payload, dict)
                and required.issubset(payload)
                and all(payload[key] is True for key in required)
            ):
                return payload
            last_error = RuntimeError("OMP Deck health check reported an unhealthy service")
        except (
            OSError,
            urllib.error.URLError,
            json.JSONDecodeError,
        ) as error:
            last_error = error
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(0.1, remaining))
    raise RuntimeError(f"OMP Deck health check failed: {last_error}") from last_error


def plugin_status(settings, *, runner=run):
    record = plugin_record(settings.omp_bin, PLUGIN_NAME, runner=runner)
    return bool(
        record
        and record.get("version") == PLUGIN_VERSION
        and (record.get("manifest") or {}).get("version") == PLUGIN_VERSION
    )
