"""Private operator state, separate from Deck's production state."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from omp_deck._private_state import PrivateStateFile, ensure_private_directory, validate_private_file

MAX_JSON = 2 * 1024 * 1024
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}\Z")


class ReleaseError(RuntimeError):
    """An operator-safe failure; subprocess output and credentials are never included."""


def identifier(value: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ReleaseError("invalid release or target identifier")
    return value


def absolute(value: str) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ReleaseError("expected an absolute path")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ReleaseError("paths must be absolute and cannot contain traversal")
    return path


def directory(path: Path) -> Path:
    ensure_private_directory(path, repair=False)
    return path


def load(path: Path) -> dict:
    validate_private_file(path, MAX_JSON)
    storage = PrivateStateFile(path, repair_root=False)
    with storage.locked():
        payload = storage.read(MAX_JSON)
    if payload is None or len(payload) > MAX_JSON:
        raise ReleaseError("missing or oversized release state")
    value = json.loads(payload)
    if not isinstance(value, dict) or value.get("version") != 1:
        raise ReleaseError("unsupported release state version")
    return value


def save(path: Path, value: dict, *, overwrite: bool = True) -> None:
    payload = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()
    if len(payload) > MAX_JSON:
        raise ReleaseError("release state exceeds its limit")
    storage = PrivateStateFile(path, repair_root=False)
    with storage.locked():
        storage.replace(payload, overwrite=overwrite)


def default_config() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "omp-deck-release/config.json"
