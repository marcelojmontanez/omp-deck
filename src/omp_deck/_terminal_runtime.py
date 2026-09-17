"""Thread-safe ownership state for terminal-backed transports."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ._extension_activation import ExtensionEventActivation
from ._extension_protocol import ExtensionDisconnected, ExtensionRequestTimeout
from .collab_adapter import CollabAdapter
from .extension_adapter import ExtensionAdapter
from .registry_store import RegistryStore


class TerminalSessionNotConnected(LookupError):
    pass


def _open_extension(factory: Callable[[], tuple[ExtensionEventActivation, ExtensionAdapter]]):
    """Prepare a verified connection before replacing any existing owner."""

    def register(adapter: ExtensionAdapter):
        registration = adapter.start()
        snapshot = adapter.snapshot()
        if snapshot is None or snapshot.transport_status != "connected":
            raise TerminalSessionNotConnected("Extension snapshot unavailable")
        return registration, snapshot

    activation, adapter = factory()
    history_error = None
    try:
        registration, snapshot = register(adapter)
        try:
            adapter.sync_history()
        except (ExtensionDisconnected, ExtensionRequestTimeout):
            adapter.close()
            activation, adapter = factory()
            registration, snapshot = register(adapter)
            history_error = "The extension could not synchronize conversation history"
        return activation, adapter, registration, snapshot, history_error
    except Exception:
        adapter.close()
        raise


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


def retire_extension(
    runtimes: _RuntimeStore,
    lock_for: Callable[[str], threading.RLock],
    model: Any,
    session_id: str,
    adapter: ExtensionAdapter | None,
    reason: str,
) -> None:
    if reason == "closed":
        return
    current = runtimes.get(session_id)
    if current is None or current.adapter is not adapter:
        return
    with lock_for(session_id):
        if not runtimes.remove_if_current(current):
            return
        model.set_transport(
            session_id,
            instance_id=None,
            generation=current.generation,
            status="disconnected",
            reason=reason,
        )


def _registry_record(registry: RegistryStore, session_id: str) -> dict[str, object]:
    for record in registry.load():
        if record["id"] == session_id:
            return record
    raise KeyError(session_id)


def _upsert_extension_record(
    registry: RegistryStore,
    session_id: str,
    metadata: Mapping[str, Any],
    project_root: Path,
    connection_id: str,
) -> dict[str, object]:
    raw_title = metadata.get("title")
    title = raw_title if isinstance(raw_title, str) and raw_title else "Terminal session"
    raw_project = metadata.get("project")
    project_path = raw_project if isinstance(raw_project, str) else str(project_root)
    adapter_kind = "extension"
    omp_session_id = str(metadata["sessionId"])
    project_name = Path(project_path).name or "Terminal"
    recovery_metadata = {"connectionId": connection_id}
    try:
        _registry_record(registry, session_id)
    except KeyError:
        return registry.create(
            session_id=session_id,
            adapter_kind=adapter_kind,
            omp_session_id=omp_session_id,
            project_name=project_name,
            project_path=project_path,
            title=title,
            recovery_metadata=recovery_metadata,
        )
    return registry.update(
        session_id,
        adapter_kind=adapter_kind,
        omp_session_id=omp_session_id,
        project_name=project_name,
        project_path=project_path,
        title=title,
        recovery_metadata=recovery_metadata,
    )
