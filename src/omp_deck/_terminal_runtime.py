"""Thread-safe ownership state for terminal-backed transports."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

from .collab_adapter import CollabAdapter
from .extension_adapter import ExtensionAdapter
from .registry_store import RegistryStore


class TerminalSessionNotConnected(LookupError):
    pass


@dataclass(slots=True)
class _Runtime:
    session_id: str
    instance_id: str
    generation: int
    kind: str
    adapter: CollabAdapter | ExtensionAdapter
    socket_path: Path | None = None


class _RuntimeStore:
    """Thread-safe ownership map for active terminal transports."""

    def __init__(self, lock: threading.RLock) -> None:
        self._lock = lock
        self._items: dict[str, _Runtime] = {}

    def get(self, session_id: str) -> _Runtime | None:
        with self._lock:
            return self._items.get(session_id)

    def pop(self, session_id: str) -> _Runtime | None:
        with self._lock:
            return self._items.pop(session_id, None)

    def require(self, session_id: str) -> _Runtime:
        runtime = self.get(session_id)
        if runtime is None:
            raise TerminalSessionNotConnected(session_id)
        return runtime

    def values(self) -> tuple[_Runtime, ...]:
        with self._lock:
            return tuple(self._items.values())

    def install(self, runtime: _Runtime) -> _Runtime | None:
        with self._lock:
            previous = self._items.get(runtime.session_id)
            self._items[runtime.session_id] = runtime
            return previous

    def is_current(self, runtime: _Runtime) -> bool:
        with self._lock:
            return self._items.get(runtime.session_id) is runtime

    def remove_if_current(self, runtime: _Runtime) -> bool:
        with self._lock:
            if self._items.get(runtime.session_id) is not runtime:
                return False
            self._items.pop(runtime.session_id)
            return True


def _registry_record(registry: RegistryStore, session_id: str) -> dict[str, object]:
    for record in registry.load():
        if record["id"] == session_id:
            return record
    raise KeyError(session_id)
