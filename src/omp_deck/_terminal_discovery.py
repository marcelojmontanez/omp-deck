"""Private validation and discovery for terminal-owned extension sockets."""
from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

_MAX_METADATA_BYTES = 64 * 1024
_OpenFile = Callable[[Path, int], int]


def terminal_id(omp_session_id: str) -> str:
    digest = hashlib.sha256(omp_session_id.encode("utf-8")).hexdigest()[:24]
    return f"terminal:{digest}"


def discover_extension_candidates(
    root: Path,
    *,
    open_file: _OpenFile = os.open,
    effective_uid: int | None = None,
) -> dict[str, tuple[Path, dict[str, Any]]]:
    """Return the newest securely validated registration for each OMP session."""
    uid = os.geteuid() if effective_uid is None else effective_uid
    if not _safe_discovery_root(root, uid):
        return {}
    candidates: dict[str, tuple[Path, dict[str, Any], int]] = {}
    for metadata_path in root.glob("*.sock.json"):
        candidate = _validated_candidate(metadata_path, open_file, uid)
        if candidate is None:
            continue
        session_id, socket_path, metadata, modified_ns = candidate
        prior = candidates.get(session_id)
        if prior is None or modified_ns > prior[2]:
            candidates[session_id] = (socket_path, metadata, modified_ns)
    return {
        session_id: (socket_path, metadata)
        for session_id, (socket_path, metadata, _) in candidates.items()
    }


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
        if not _nonempty_string(session_id) or not _nonempty_string(connection_id):
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
    socket_id = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:24]
    return (
        stat.S_ISSOCK(info.st_mode)
        and info.st_uid == uid
        and socket_path.name.startswith(socket_id + ".")
        and socket_path.name.endswith("." + connection_id + ".sock")
    )


def _nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value)
