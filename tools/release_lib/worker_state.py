from __future__ import annotations

import json
from pathlib import Path

from omp_deck._private_state import PrivateStateFile


def write_json(path: Path, value: dict) -> None:
    data = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    storage = PrivateStateFile(path, repair_root=False)
    with storage.locked():
        storage.replace(data)


def read_json(path: Path, *, limit: int = 1024 * 1024) -> dict:
    storage = PrivateStateFile(path, repair_root=False)
    with storage.locked():
        data = storage.read(limit)
    if data is None:
        raise FileNotFoundError(path)
    if len(data) > limit:
        raise RuntimeError("release state exceeded limit")
    value = json.loads(data)
    if not isinstance(value, dict):
        raise RuntimeError("invalid release state")
    return value
