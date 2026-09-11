"""Private validation and discovery for terminal-owned extension sockets."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ._extension_endpoint import unix_endpoint_name

_MAX_METADATA_BYTES = 64 * 1024
_OpenFile = Callable[[Path, int], int]


def terminal_id(omp_session_id: str) -> str:
    digest = hashlib.sha256(omp_session_id.encode("utf-8")).hexdigest()[:24]
    return f"terminal:{digest}"


def removed_extension_connection(record: dict[str, Any]) -> str | None:
    if record.get("adapterKind") != "extension":
        return None
    metadata = record.get("recoveryMetadata")
    if not isinstance(metadata, dict) or metadata.get("removed") is not True:
        return None
    connection_id = metadata.get("connectionId")
    return connection_id if isinstance(connection_id, str) else None


def discover_extension_candidates(
    root: Path,
    *,
    open_file: _OpenFile = os.open,
    effective_uid: int | None = None,
    all_candidates: bool = False,
) -> dict[str, Any]:
    """Return securely validated registrations, newest first when requested."""
    if os.name == "nt":
        return _discover_windows_candidates(root, all_candidates=all_candidates)
    uid = os.geteuid() if effective_uid is None else effective_uid
    if not _safe_discovery_root(root, uid):
        return {}
    candidates: dict[str, list[tuple[Path, dict[str, Any], int]]] = {}
    for metadata_path in root.glob("*.sock.json"):
        candidate = _validated_candidate(metadata_path, open_file, uid)
        if candidate is None:
            continue
        session_id, socket_path, metadata, modified_ns = candidate
        candidates.setdefault(session_id, []).append((socket_path, metadata, modified_ns))
    ordered = {
        session_id: sorted(values, key=lambda item: item[2], reverse=True) for session_id, values in candidates.items()
    }
    if all_candidates:
        return {
            session_id: tuple((socket_path, metadata) for socket_path, metadata, _ in values)
            for session_id, values in ordered.items()
        }
    return {session_id: (values[0][0], values[0][1]) for session_id, values in ordered.items()}


def _discover_windows_candidates(root: Path, *, all_candidates: bool = False) -> dict[str, Any]:
    from ._windows_security import is_private_path, owned_by_current_user

    if not is_private_path(root):
        return {}
    candidates: dict[str, list[tuple[Path, dict[str, Any], int]]] = {}
    for metadata_path in root.glob("*.pipe.json"):
        try:
            info = metadata_path.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & 0x0400
                or info.st_size > _MAX_METADATA_BYTES
                or not owned_by_current_user(metadata_path)
            ):
                continue
            with metadata_path.open("rb") as stream:
                raw = stream.read(_MAX_METADATA_BYTES + 1)
            if len(raw) > _MAX_METADATA_BYTES:
                continue
            metadata = json.loads(raw)
            session_id = metadata.get("sessionId")
            connection_id = metadata.get("connectionId")
            endpoint = metadata.get("endpoint")
            pid = metadata.get("pid")
            auth_token = metadata.get("authToken")
            if (
                metadata.get("protocol") != "omp-deck-fallback-v1"
                or metadata.get("version") != 1
                or metadata.get("type") != "registration"
                or not all(_nonempty_string(value) for value in (session_id, connection_id, endpoint))
                or not isinstance(auth_token, str)
                or len(auth_token) != 64
                or any(character not in "0123456789abcdef" for character in auth_token)
                or len(endpoint) > 256
                or type(pid) is not int
                or pid <= 0
            ):
                continue
            socket_id = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:24]
            expected_name = f"{socket_id}.{pid}.{connection_id}.pipe.json"
            expected_endpoint = rf"\\.\pipe\{expected_name[:-5]}"
            if metadata_path.name != expected_name or endpoint != expected_endpoint:
                continue
            candidates.setdefault(session_id, []).append((Path(endpoint), metadata, info.st_mtime_ns))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
            continue
    ordered = {
        session_id: sorted(values, key=lambda item: item[2], reverse=True) for session_id, values in candidates.items()
    }
    if all_candidates:
        return {
            session_id: tuple((endpoint, metadata) for endpoint, metadata, _ in values)
            for session_id, values in ordered.items()
        }
    return {session_id: (values[0][0], values[0][1]) for session_id, values in ordered.items()}


def _safe_discovery_root(root: Path, uid: int) -> bool:
    try:
        info = root.stat(follow_symlinks=False)
    except FileNotFoundError:
        return False
    return (
        stat.S_ISDIR(info.st_mode)
        and not stat.S_ISLNK(info.st_mode)
        and info.st_uid == uid
        and not info.st_mode & 0o077
    )


def _validated_candidate(
    metadata_path: Path,
    open_file: _OpenFile,
    uid: int,
) -> tuple[str, Path, dict[str, Any], int] | None:
    try:
        metadata, modified_ns = _read_metadata(metadata_path, open_file, uid)
        session_id = metadata.get("sessionId")
        connection_id = metadata.get("connectionId")
        if (
            metadata.get("protocol") != "omp-deck-fallback-v1"
            or metadata.get("version") != 1
            or metadata.get("type") != "registration"
            or not _nonempty_string(session_id)
            or not _nonempty_string(connection_id)
        ):
            return None
        socket_path = Path(str(metadata_path)[:-5])
        if not _valid_socket(socket_path, session_id, connection_id, uid):
            return None
        return session_id, socket_path, metadata, modified_ns
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None


def _read_metadata(
    metadata_path: Path,
    open_file: _OpenFile,
    uid: int,
) -> tuple[dict[str, Any], int]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = open_file(metadata_path, flags)
    try:
        info = os.fstat(descriptor)
        if not _valid_metadata_info(info, uid):
            raise ValueError("unsafe extension metadata")
        raw = os.read(descriptor, _MAX_METADATA_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(raw) > _MAX_METADATA_BYTES:
        raise ValueError("extension metadata is too large")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("extension metadata must be an object")
    return value, info.st_mtime_ns


def _valid_metadata_info(info: os.stat_result, uid: int) -> bool:
    return (
        stat.S_ISREG(info.st_mode)
        and info.st_uid == uid
        and not info.st_mode & 0o077
        and info.st_size <= _MAX_METADATA_BYTES
    )


def _valid_socket(socket_path: Path, session_id: str, connection_id: str, uid: int) -> bool:
    info = socket_path.stat(follow_symlinks=False)
    return (
        stat.S_ISSOCK(info.st_mode)
        and info.st_uid == uid
        and socket_path.name == unix_endpoint_name(session_id, connection_id)
    )


def _nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value) and "\0" not in value
