from __future__ import annotations

import json
import os
from pathlib import Path

from ._private_state import PrivateStateFile
from .fleet_protocol import validate_enrollment

_MAX_TRUST_BYTES = 8 * 1024


class FleetTrustStore:
    """Private, reload-on-use enrollment state for a local Deck node."""

    def __init__(self, state_root: str | os.PathLike[str]):
        self.path = Path(state_root) / "fleet-node.json"
        self._storage = PrivateStateFile(self.path, lock_name=".fleet-node.lock", repair_root=os.name == "nt")

    def load(self) -> dict | None:
        if not self.path.exists() and not self.path.is_symlink():
            return None
        with self._storage.locked():
            return self._read_locked()

    def join(self, enrollment: dict) -> dict:
        normalized = validate_enrollment(enrollment)
        encoded = _encode(normalized)
        with self._storage.locked():
            current = self._read_locked()
            if current is not None and current != normalized:
                raise RuntimeError("already enrolled with a different fleet coordinator; leave first")
            if current is None:
                self._storage.replace(encoded)
        return normalized

    def leave(self) -> bool:
        if not self.path.exists() and not self.path.is_symlink():
            return False
        with self._storage.locked():
            return self._storage.delete()

    def _read_locked(self) -> dict | None:
        raw = self._storage.read(_MAX_TRUST_BYTES)
        if raw is None:
            return None
        if len(raw) > _MAX_TRUST_BYTES:
            raise RuntimeError("fleet trust file is too large")
        try:
            value = json.loads(raw)
            return validate_enrollment(value)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as error:
            raise RuntimeError("fleet trust file is invalid") from error


def _encode(enrollment: dict) -> bytes:
    payload = json.dumps(enrollment, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    if len(payload) > _MAX_TRUST_BYTES:
        raise ValueError("fleet enrollment is too large")
    return payload
