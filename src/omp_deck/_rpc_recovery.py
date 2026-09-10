from __future__ import annotations

import json
import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TextIO

_MAX_HEADER_LINE_CHARS = 64 * 1024


def recovery_session_file(session_root: Path, record: Mapping[str, Any]) -> str | None:
    omp_session_id = record.get("ompSessionId")
    if omp_session_id is None:
        return None
    recovery = record.get("recoveryMetadata")
    session_file = recovery.get("sessionFile") if isinstance(recovery, Mapping) else None
    return validated_session_file(session_root, session_file, omp_session_id)


def validate_recovered_identity(
    session_root: Path,
    record: Mapping[str, Any],
    state: Mapping[str, Any],
    resume_file: str,
) -> None:
    if state.get("session_id") != record.get("ompSessionId"):
        raise RuntimeError("OMP resumed a different session identity")
    active_file = validated_session_file(session_root, state.get("session_file"), state.get("session_id"))
    if active_file != resume_file:
        raise RuntimeError("OMP resumed a different session file")


def validated_session_file(session_root: Path, session_file: object, expected_session_id: object) -> str:
    """Return a canonical, rooted OMP session file with the expected header identity."""
    if not isinstance(session_file, str) or not session_file:
        raise ValueError("RPC session state does not include a session file")
    if not isinstance(expected_session_id, str) or not expected_session_id:
        raise ValueError("RPC session state does not include a session ID")

    candidate = Path(session_file)
    if not candidate.is_absolute():
        raise ValueError("RPC session file must be an absolute path")
    try:
        root = session_root.resolve(strict=True)
    except OSError as error:
        raise ValueError("RPC session root is missing or inaccessible") from error
    if not root.is_dir():
        raise ValueError("RPC session root is not a directory")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise ValueError("registered RPC session file is missing or inaccessible") from error
    try:
        resolved.relative_to(root)
    except ValueError:
        raise ValueError("registered RPC session file is outside the configured session root") from None
    if candidate != resolved:
        raise ValueError("registered RPC session file must not use filesystem aliases")
    if resolved.suffix != ".jsonl" or not resolved.is_file():
        raise ValueError("registered RPC session file is not an OMP session file")

    header = _read_session_header(resolved)
    actual_session_id = header.get("id")
    if actual_session_id != expected_session_id:
        raise ValueError("registered RPC session file does not match its OMP session ID")
    return str(resolved)


def _read_session_header(session_file: Path) -> dict[str, Any]:
    descriptor = None
    try:
        descriptor = os.open(session_file, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        info = os.fstat(descriptor)
        current = session_file.stat(follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
            raise ValueError("session file changed while it was opened")
        with os.fdopen(descriptor, "r", encoding="utf-8") as source:
            descriptor = None
            first = _read_record(source)
            header = _read_record(source) if first.get("type") == "title" else first
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("registered RPC session file has a missing or malformed header") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if header.get("type") != "session" or not isinstance(header.get("id"), str) or not header["id"]:
        raise ValueError("registered RPC session file has a missing or malformed header")
    return header


def _read_record(source: TextIO) -> dict[str, Any]:
    line = source.readline(_MAX_HEADER_LINE_CHARS + 1)
    if not line or (len(line) > _MAX_HEADER_LINE_CHARS and not line.endswith("\n")):
        raise ValueError("missing or oversized session header")
    value = json.loads(line)
    if not isinstance(value, dict):
        raise ValueError("session header is not an object")
    return value
