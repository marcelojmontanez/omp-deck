import json
import os
import re
import stat
from pathlib import Path
from typing import Any

from ._terminal_discovery import terminal_id
from .installation_paths import OWNERSHIP_MARKER, has_ownership_marker, installation_paths
from .read_cursors import CursorStore
from .registry_store import RegistryStore

_LEGACY_METADATA = re.compile(r"[0-9a-f-]{36}\.\d+\.[^.]+\.sock\.json")
_MAX_METADATA_BYTES = 64 * 1024
_MAX_CURSOR_BYTES = 1024 * 1024
_RECOVERY_FILE = re.compile(
    r"(?:"
    r"auth\.json\.corrupt-\d+-[0-9a-f]{8}|"
    r"(?:registry|collab)\.json\.corrupt\.(?:\d+|unknown)(?:\.\d+)?|"
    r"cursors\.json\.corrupt-\d{8}T\d{12}Z|"
    r"\.auth\.json\.[0-9a-f]{16}\.tmp|"
    r"\.(?:registry|requests|collab|cursors)\.json\.[0-9a-f]{32}\.tmp"
    r")"
)


def migration_plan(settings) -> dict[str, int | bool]:
    candidates = _legacy_sessions(settings)
    cursor_path = settings.state_root / "read-cursors.json"
    cursors = _legacy_cursors(cursor_path)
    return {
        "configurationReused": True,
        "legacySessions": len(candidates),
        "legacyCursorFilePresent": cursor_path.exists(),
        "legacyCursorFileAccepted": _owned_file_safe(cursor_path, allow_legacy_readable=True),
        "legacyReadCursors": sum(1 for session_id in candidates if session_id in cursors),
        "registryExists": (settings.state_root / "registry.json").exists(),
        "cursorStoreExists": settings.cursor_path.exists(),
    }


def apply_migration(settings) -> dict[str, int | bool]:
    candidates = _legacy_sessions(settings)
    _validate_adoptable_state_root(settings)
    cursor_path = settings.state_root / "read-cursors.json"
    cursors = _legacy_cursors(cursor_path)
    registry = RegistryStore(settings.state_root / "registry.json")
    existing = {record["id"] for record in registry.load()}
    imported = 0
    for omp_session_id, metadata in candidates.items():
        session_id = terminal_id(omp_session_id)
        if session_id in existing:
            continue
        project_path = _validated_project(metadata.get("cwd"), settings.project_root)
        if project_path is None:
            continue
        registry.create(
            adapter_kind="extension",
            project_name=project_path.name,
            project_path=str(project_path),
            title=_title(metadata, project_path.name),
            omp_session_id=omp_session_id,
            recovery_metadata={"source": "legacy-extension-registration"},
            session_id=session_id,
        )
        existing.add(session_id)
        imported += 1

    cursor_store = CursorStore(settings.cursor_path)
    importable_cursors = {
        terminal_id(omp_session_id): event_id
        for omp_session_id, event_id in cursors.items()
        if terminal_id(omp_session_id) in existing
    }
    migrated_cursors = cursor_store.import_missing(importable_cursors)
    _mark_state_root_owned(settings.state_root)
    return {
        "configurationReused": True,
        "sessionsImported": imported,
        "legacyCursorFileAccepted": _owned_file_safe(cursor_path, allow_legacy_readable=True),
        "readCursorsImported": migrated_cursors,
        "cursorStoreHealthy": cursor_store.healthy,
    }


def _legacy_sessions(settings) -> dict[str, dict[str, Any]]:
    root = settings.socket_root
    if not _private_directory(root):
        return {}
    result = {}
    for path in root.glob("*.sock.json"):
        if _LEGACY_METADATA.fullmatch(path.name) is None:
            continue
        value = _read_owned_json(path, _MAX_METADATA_BYTES)
        if not isinstance(value, dict):
            continue
        session_id = value.get("sessionId")
        if isinstance(session_id, str) and 0 < len(session_id) <= 128:
            result[session_id] = value
    return result


def _legacy_cursors(path: Path) -> dict[str, str]:
    value = _read_owned_json(path, _MAX_CURSOR_BYTES, allow_legacy_readable=True)
    if not isinstance(value, dict) or value.get("version") != 1:
        return {}
    sessions = value.get("sessions")
    if not isinstance(sessions, dict) or len(sessions) > 10_000:
        return {}
    result = {}
    for session_id, entry in sessions.items():
        event_id = entry.get("eventId") if isinstance(entry, dict) else None
        if (
            isinstance(session_id, str)
            and 0 < len(session_id) <= 128
            and isinstance(event_id, str)
            and 0 < len(event_id) <= 512
        ):
            result[session_id] = event_id
    return result


def _read_owned_json(path: Path, limit: int, *, allow_legacy_readable: bool = False):
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    try:
        info = os.fstat(descriptor)
        unsafe_permissions = info.st_mode & (0o002 if allow_legacy_readable else 0o077)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or unsafe_permissions or info.st_size > limit:
            return None
        raw = os.read(descriptor, limit + 1)
        if len(raw) > limit:
            return None
        return json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    finally:
        os.close(descriptor)


def _owned_file_safe(path: Path, *, allow_legacy_readable: bool = False) -> bool:
    try:
        info = path.stat(follow_symlinks=False)
    except OSError:
        return False
    unsafe_permissions = info.st_mode & (0o002 if allow_legacy_readable else 0o077)
    return (
        stat.S_ISREG(info.st_mode)
        and not stat.S_ISLNK(info.st_mode)
        and info.st_uid == os.geteuid()
        and not unsafe_permissions
    )


def _validate_adoptable_state_root(settings) -> None:
    root = settings.state_root
    approved = installation_paths(state_root=root).state_root
    if root.absolute() != approved or root.is_symlink():
        raise RuntimeError("legacy state root is not an approved Deck path")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root_info = root.stat(follow_symlinks=False)
    if not stat.S_ISDIR(root_info.st_mode) or root_info.st_uid != os.geteuid() or root_info.st_mode & 0o077:
        raise RuntimeError("legacy state root has unsafe ownership or permissions")
    allowed_directories = {"sockets", "uploads"}
    allowed_files = {
        "read-cursors.json",
        "registry.json",
        "requests.json",
        "cursors.json",
        "auth.json",
        "collab.json",
        ".auth.lock",
        ".registry.json.lock",
        ".requests.json.lock",
        ".collab.json.lock",
        ".cursors.json.lock",
        OWNERSHIP_MARKER,
    }
    count = 0
    pending = [root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                count += 1
                if count > 10_000:
                    raise RuntimeError("legacy state root has too many entries")
                path = Path(entry.path)
                info = entry.stat(follow_symlinks=False)
                if info.st_uid != os.geteuid() or stat.S_ISLNK(info.st_mode):
                    raise RuntimeError("legacy state root contains unsafe entries")
                if directory == root:
                    if entry.is_dir(follow_symlinks=False) and entry.name in allowed_directories:
                        pending.append(path)
                    elif entry.is_file(follow_symlinks=False) and (
                        entry.name in allowed_files or (_RECOVERY_FILE.fullmatch(entry.name) and _owned_file_safe(path))
                    ):
                        continue
                    else:
                        raise RuntimeError("legacy state root contains unknown entries")
                elif entry.is_dir(follow_symlinks=False):
                    raise RuntimeError("legacy state directories must not be nested")
                elif not (entry.is_file(follow_symlinks=False) or stat.S_ISSOCK(info.st_mode)):
                    raise RuntimeError("legacy state root contains unsupported entries")


def _mark_state_root_owned(root: Path) -> None:
    if has_ownership_marker(root):
        return
    descriptor = os.open(
        root / OWNERSHIP_MARKER,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.write(descriptor, b"OMP Deck owned directory\n")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory_fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _private_directory(path: Path) -> bool:
    try:
        info = path.stat(follow_symlinks=False)
    except OSError:
        return False
    return (
        stat.S_ISDIR(info.st_mode)
        and not stat.S_ISLNK(info.st_mode)
        and info.st_uid == os.geteuid()
        and not info.st_mode & 0o077
    )


def _validated_project(value, root: Path) -> Path | None:
    if not isinstance(value, str):
        return None
    try:
        resolved_root = root.resolve(strict=True)
        candidate = Path(value).resolve(strict=True)
        candidate.relative_to(resolved_root)
    except (OSError, ValueError):
        return None
    return candidate if candidate.is_dir() else None


def _title(metadata: dict[str, Any], fallback: str) -> str:
    value = metadata.get("title")
    return value[:200] if isinstance(value, str) and value else fallback
