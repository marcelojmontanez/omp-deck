import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from ._platform_paths import default_plugin_root
from ._plugin_inventory import plugin_record
from .installation_paths import (
    mark_owned,
    remove_owned_root,
    validate_owned_root,
    validate_ownership_claim,
)

PLUGIN_VERSION = "1.3.6"
PLUGIN_NAME = "omp-deck"
PLUGIN_ROOT = Path(__file__).resolve().parent / "plugin"


def run(command, *, check=True, capture_output=False):
    """Keep native diagnostics out of Deck's machine-readable standard output."""
    return subprocess.run(
        command,
        check=check,
        stdout=subprocess.PIPE if capture_output else sys.stderr,
        stderr=subprocess.PIPE if capture_output else None,
        text=True,
    )


def plugin_storage_root():
    return Path(os.path.abspath(default_plugin_root().expanduser()))


def _prepare_plugin_storage():
    root = plugin_storage_root()
    validate_ownership_claim(root)
    mark_owned(root, mode=0o700 if os.name != "nt" else None)
    return root


def _stored_plugin(path, root=None):
    root = plugin_storage_root() if root is None else Path(root)
    try:
        resolved_root = root.resolve(strict=True)
        resolved = Path(path).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if (
        resolved.parent != resolved_root
        or not resolved.is_dir()
        or Path(path).is_symlink()
        or not resolved.name.startswith(("plugin-", "rollback-"))
    ):
        return None
    return resolved


def authorized_plugin_source(path):
    root = plugin_storage_root()
    try:
        validate_owned_root(root, (root,))
    except (OSError, RuntimeError):
        return None
    return _stored_plugin(path, root)


def _same_source(first, second):
    try:
        return Path(first).resolve(strict=True) == Path(second).resolve(strict=True)
    except (OSError, TypeError, ValueError):
        return False


def _record_source(record):
    if record is None:
        return None
    try:
        return Path(record["path"]).resolve(strict=True)
    except (KeyError, TypeError, ValueError, OSError):
        return None


def copy_plugin(source, *, prefix):
    source = Path(source)
    if not source.is_dir() or source.is_symlink() or not (source / "package.json").is_file():
        raise RuntimeError("installed OMP Deck plugin source is unsafe")
    for directory, directories, files in os.walk(source):
        directories[:] = [name for name in directories if name != "node_modules"]
        for name in (*directories, *files):
            entry = Path(directory) / name
            if entry.is_symlink() or not (entry.is_file() or entry.is_dir()):
                raise RuntimeError("installed OMP Deck plugin source contains an unsafe entry")
    root = _prepare_plugin_storage()
    temporary = Path(tempfile.mkdtemp(prefix=".plugin-stage-", dir=root))
    destination = root / f"{prefix}{temporary.name.removeprefix('.plugin-stage-')}"
    try:
        shutil.copytree(
            source,
            temporary,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("node_modules"),
        )
        os.replace(temporary, destination)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        shutil.rmtree(destination, ignore_errors=True)
        raise
    return destination


def _remove_stored_plugin(path):
    root = plugin_storage_root()
    stored = _stored_plugin(path, root)
    if stored is None:
        return
    validate_owned_root(root, (root,))
    shutil.rmtree(stored)


def plugin_installed(settings, *, runner=None):
    return plugin_record(settings.omp_bin, PLUGIN_NAME, runner=runner or run) is not None


def backup_plugin(settings, *, runner=None):
    record = plugin_record(settings.omp_bin, PLUGIN_NAME, runner=runner or run)
    source = _record_source(record)
    if source is None:
        return None
    stored = _stored_plugin(source)
    if stored is not None:
        return stored
    return copy_plugin(source, prefix="rollback-")


def restore_plugin(settings, backup, *, runner=None):
    runner = runner or run
    if backup is None:
        return remove_plugin(settings.omp_bin, runner=runner)
    current = plugin_record(settings.omp_bin, PLUGIN_NAME, runner=runner)
    current_source = _record_source(current)
    result = runner([settings.omp_bin, "plugin", "install", str(backup)])
    if current_source is not None and not _same_source(current_source, backup):
        _remove_stored_plugin(current_source)
    return result


def discard_plugin_backup(backup):
    if backup is not None:
        _remove_stored_plugin(backup)


def install_plugin(settings, *, runner=run, preserve=None, source=None):
    if os.name == "nt":
        from ._private_state import ensure_private_directory

        ensure_private_directory(settings.socket_root)
    source_root = PLUGIN_ROOT if source is None else Path(source)
    if not (source_root / "package.json").is_file():
        raise RuntimeError("OMP Deck plugin source is missing or invalid")
    preserved_source = authorized_plugin_source(preserve) if preserve is not None else None
    if preserve is not None and preserved_source is None:
        raise RuntimeError("preserved plugin source must be an owned durable installation")
    previous = plugin_record(settings.omp_bin, PLUGIN_NAME, runner=runner)
    previous_source = _record_source(previous)
    installed_source = copy_plugin(source_root, prefix="plugin-")
    try:
        manifest = json.loads((installed_source / "package.json").read_text(encoding="utf-8"))
        expected_version = manifest["version"]
        valid_manifest = (
            isinstance(manifest, dict)
            and manifest.get("name") == PLUGIN_NAME
            and isinstance(expected_version, str)
            and bool(expected_version)
            and (source is not None or expected_version == PLUGIN_VERSION)
        )
    except (KeyError, OSError, TypeError, json.JSONDecodeError):
        valid_manifest = False
    if not valid_manifest:
        _remove_stored_plugin(installed_source)
        raise RuntimeError("OMP Deck plugin source is missing or invalid")
    runner([settings.omp_bin, "plugin", "install", str(installed_source)])
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
    installed = plugin_record(settings.omp_bin, PLUGIN_NAME, runner=runner)
    if (
        not isinstance(checks, list)
        or not isinstance(plugin_check, dict)
        or plugin_check.get("status") != "ok"
        or installed is None
        or installed.get("version") != expected_version
        or (installed.get("manifest") or {}).get("version") != expected_version
        or not _same_source(installed.get("path"), installed_source)
    ):
        raise RuntimeError("OMP Deck plugin validation failed")
    if previous_source is not None and previous_source != preserved_source:
        _remove_stored_plugin(previous_source)
    return installed_source


def remove_plugin(omp_bin="omp", *, runner=run):
    record = plugin_record(omp_bin, PLUGIN_NAME, runner=runner)
    if record is None:
        return None
    source = _record_source(record)
    result = runner([omp_bin, "plugin", "uninstall", PLUGIN_NAME])
    if source is not None:
        _remove_stored_plugin(source)
    root = plugin_storage_root()
    if root.exists():
        remove_owned_root(root, (root,))
    return result


def plugin_status(settings, *, runner=run):
    record = plugin_record(settings.omp_bin, PLUGIN_NAME, runner=runner)
    return bool(
        record
        and record.get("version") == PLUGIN_VERSION
        and (record.get("manifest") or {}).get("version") == PLUGIN_VERSION
    )
