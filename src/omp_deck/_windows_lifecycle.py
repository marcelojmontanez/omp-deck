from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

from ._private_state import ensure_private_directory
from ._windows_security import current_user_sid_string, is_private_path, secure_created_path, secure_path
from .installation_paths import (
    InstallationPaths,
    has_ownership_marker,
    installation_paths,
    manifest_content,
    mark_owned,
    remove_owned_root,
    validate_manifest,
)

_TASK_NAME = "OMP Deck"
_TASK_PATH = rf"\{_TASK_NAME}"
_QUERY = ("schtasks.exe", "/Query", "/TN", _TASK_PATH)
_STATE_COMMAND = "(Get-ScheduledTask -TaskName 'OMP Deck' -ErrorAction Stop).State.ToString()"
_ALLOWED_STATE_FILES = {
    ".auth.lock",
    ".collab.json.lock",
    ".cursors.json.lock",
    ".registry.json.lock",
    ".requests.json.lock",
    "auth.json",
    "collab.json",
    "cursors.json",
    "registry.json",
    "requests.json",
}


@dataclass(frozen=True)
class _RootSnapshot:
    path: Path
    existed: bool
    marker_existed: bool
    entries: tuple[str, ...]


@dataclass(frozen=True)
class WindowsServiceSnapshot:
    task_xml: bytes | None
    manifest: bytes | None
    config: bytes | None
    enabled: bool
    active: bool
    installation: InstallationPaths
    roots: tuple[_RootSnapshot, ...]


def _result(runner, command, *, check=True, capture_output=False):
    return runner(list(command), check=check, capture_output=capture_output)


def _query_task(runner):
    return _result(runner, (*_QUERY, "/XML"), check=False, capture_output=True)


def _task_enabled(xml: bytes | None) -> bool:
    if xml is None:
        return False
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return False
    enabled = root.find("{*}Settings/{*}Enabled")
    return enabled is None or enabled.text != "false"


def _task_active(runner) -> bool:
    result = _result(
        runner,
        ("powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _STATE_COMMAND),
        check=False,
        capture_output=True,
    )
    return result.returncode == 0 and result.stdout.strip().casefold() == "running"


def _file_bytes(path: Path) -> bytes | None:
    if not path.exists():
        return None
    if not path.is_file() or path.is_symlink() or not is_private_path(path):
        raise RuntimeError(f"cannot snapshot unsafe installation file: {path}")
    return path.read_bytes()


def _root_snapshot(path: Path) -> _RootSnapshot:
    existed = path.is_dir() and not path.is_symlink()
    return _RootSnapshot(
        path,
        existed,
        has_ownership_marker(path) if existed else False,
        tuple(sorted(item.name for item in path.iterdir())) if existed else (),
    )


def capture_service_state(*, settings=None, config_path=None, runner) -> WindowsServiceSnapshot:
    paths = (
        installation_paths(config_file=config_path, state_root=settings.state_root)
        if settings is not None and config_path is not None
        else installation_paths()
    )
    task = _query_task(runner)
    task_xml = task.stdout.lstrip("\ufeff").encode("utf-16") if task.returncode == 0 else None
    roots = tuple(
        _root_snapshot(path) for path in dict.fromkeys((paths.install_root, paths.config_root, paths.state_root))
    )
    return WindowsServiceSnapshot(
        task_xml=task_xml,
        manifest=_file_bytes(paths.manifest_path),
        config=_file_bytes(paths.config_path),
        enabled=_task_enabled(task_xml),
        active=_task_active(runner) if task_xml is not None else False,
        installation=paths,
        roots=roots,
    )


def _write_private(path: Path, content: bytes) -> None:
    ensure_private_directory(path.parent)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(16)}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        secure_created_path(temporary)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _restore_root(snapshot: _RootSnapshot) -> None:
    if not snapshot.existed:
        remove_owned_root(snapshot.path, (snapshot.path,))
        return
    if snapshot.marker_existed or not has_ownership_marker(snapshot.path):
        return
    preserved = set(snapshot.entries)
    for child in snapshot.path.iterdir():
        if child.name in preserved:
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink(missing_ok=True)


def _delete_task(runner) -> None:
    if _query_task(runner).returncode != 0:
        return
    _result(runner, ("schtasks.exe", "/End", "/TN", _TASK_PATH), check=False)
    _result(runner, ("schtasks.exe", "/Delete", "/TN", _TASK_PATH, "/F"))


def _register_task(runner, paths: InstallationPaths, task_xml: bytes) -> None:
    _write_private(paths.unit_path, task_xml)
    _result(runner, ("schtasks.exe", "/Create", "/TN", _TASK_PATH, "/XML", str(paths.unit_path), "/F"))


def restore_service_state(snapshot: WindowsServiceSnapshot, *, runner) -> None:
    paths = snapshot.installation
    _delete_task(runner)
    if snapshot.config is None:
        paths.config_path.unlink(missing_ok=True)
    else:
        _write_private(paths.config_path, snapshot.config)
    if snapshot.manifest is None:
        paths.manifest_path.unlink(missing_ok=True)
    else:
        _write_private(paths.manifest_path, snapshot.manifest)
    if snapshot.task_xml is None:
        paths.unit_path.unlink(missing_ok=True)
    else:
        _register_task(runner, paths, snapshot.task_xml)
        if not snapshot.enabled:
            _result(runner, ("schtasks.exe", "/Change", "/TN", _TASK_PATH, "/Disable"))
        if snapshot.active:
            _result(runner, ("schtasks.exe", "/Run", "/TN", _TASK_PATH))
    for root in reversed(snapshot.roots):
        _restore_root(root)


def _pythonw(executable: str) -> str:
    path = Path(executable)
    candidate = path.with_name("pythonw.exe")
    return str(candidate if candidate.is_file() else path)


def task_xml(config_path: Path, *, executable: str = sys.executable, user_sid: str | None = None) -> str:
    sid = current_user_sid_string() if user_sid is None else user_sid
    command = escape(_pythonw(executable))
    arguments = escape(subprocess.list2cmdline(["-m", "omp_deck", "serve", "--config", str(config_path)]))
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>OMP Deck local control plane</Description></RegistrationInfo>
  <Triggers><LogonTrigger><Enabled>true</Enabled><UserId>{escape(sid)}</UserId></LogonTrigger></Triggers>
  <Principals><Principal id="Author"><UserId>{escape(sid)}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <Enabled>true</Enabled>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure><Interval>PT1M</Interval><Count>3</Count></RestartOnFailure>
  </Settings>
  <Actions Context="Author"><Exec><Command>{command}</Command><Arguments>{arguments}</Arguments></Exec></Actions>
</Task>
"""


def _allowed_state_entries(settings) -> tuple[Path, ...]:
    paths = [settings.state_root / name for name in _ALLOWED_STATE_FILES]
    if settings.socket_root.parent == settings.state_root:
        paths.append(settings.socket_root)
    return tuple(paths)


def prepare_installation(settings, config_path: Path) -> InstallationPaths:
    paths = installation_paths(config_file=config_path, state_root=settings.state_root)
    mark_owned(paths.install_root)
    mark_owned(paths.config_root, allowed_existing=(paths.config_path,))
    mark_owned(paths.state_root, allowed_existing=_allowed_state_entries(settings))
    if not paths.config_path.exists():
        _write_private(paths.config_path, b"{}\n")
    else:
        secure_path(paths.config_path)
    _write_private(paths.manifest_path, manifest_content(paths).encode("utf-8"))
    ensure_private_directory(settings.socket_root)
    return paths


def install_service(settings, config_path: Path, *, runner, start: bool = True) -> InstallationPaths:
    existing = _query_task(runner)
    base_paths = installation_paths()
    if existing.returncode == 0:
        validate_manifest(base_paths)
    paths = prepare_installation(settings, config_path)
    definition = task_xml(paths.config_path).encode("utf-16")
    _register_task(runner, paths, definition)
    if start:
        _result(runner, ("schtasks.exe", "/Run", "/TN", _TASK_PATH))
    return paths


def service_status(*, runner) -> int:
    task = _query_task(runner)
    return 0 if task.returncode == 0 and _task_active(runner) else 1


def restart_service(*, runner) -> None:
    if _query_task(runner).returncode != 0:
        raise RuntimeError("OMP Deck scheduled task is not installed")
    _result(runner, ("schtasks.exe", "/End", "/TN", _TASK_PATH), check=False)
    _result(runner, ("schtasks.exe", "/Run", "/TN", _TASK_PATH))


def remove_service(*, runner) -> InstallationPaths:
    paths = validate_manifest(installation_paths())
    _delete_task(runner)
    paths.unit_path.unlink(missing_ok=True)
    return paths
