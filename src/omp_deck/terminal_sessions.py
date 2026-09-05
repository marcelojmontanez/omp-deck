"""Ownership and supervision for terminal-owned OMP transports."""

from __future__ import annotations

import os
import threading
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import _terminal_commands
from ._collab_imports import CollabImportCoordinator
from ._shutdown import close_terminal_manager
from ._terminal_discovery import discover_extension_candidates, removed_extension_connection
from ._terminal_discovery import terminal_id as _terminal_id
from ._terminal_runtime import TerminalSessionNotConnected, _registry_record, _Runtime, _RuntimeStore
from .collab_adapter import CollabAdapter
from .collab_store import CollabStore
from .extension_adapter import ExtensionAdapter
from .fleet_model import FleetModel
from .registry_store import RegistryStore
from .request_ledger import RequestLedger
from .rpc_adapter import AdapterEvent
from .settings import Settings

_DISCOVERY_INTERVAL = 2.0


class TerminalSessionManager:
    """Connects registered extension sockets and explicit Collab credentials."""

    def __init__(
        self,
        settings: Settings,
        model: FleetModel,
        *,
        registry: RegistryStore | None = None,
        credentials: CollabStore | None = None,
        requests: RequestLedger | None = None,
        discovery_interval: float = _DISCOVERY_INTERVAL,
    ) -> None:
        if discovery_interval <= 0:
            raise ValueError("discovery_interval must be positive")
        self.settings = settings
        self.model = model
        self.registry = registry or RegistryStore(settings.state_root / "registry.json")
        self.credentials = credentials or CollabStore(settings.state_root)
        self.requests = requests or RequestLedger(settings.state_root / "requests.json")
        self.discovery_interval = discovery_interval
        self._lock = threading.RLock()
        self._runtimes = _RuntimeStore(self._lock)
        self._commands = _terminal_commands.TerminalCommandCoordinator(
            self.model,
            self.requests,
        )
        self._collab_imports = CollabImportCoordinator(
            self.requests,
            self.registry,
            self.credentials,
            self.model,
            settings.project_root,
            self._connect_collab,
            self._runtimes.require,
            self._rollback_collab,
        )
        self._stop = threading.Event()
        self._watcher: threading.Thread | None = None
        self._ignored_extensions: dict[str, str] = {}
        for record in self.registry.load():
            ignored = removed_extension_connection(record)
            if ignored is not None:
                self._ignored_extensions[record["id"]] = ignored
            elif record["adapterKind"] in {"collab", "extension"}:
                self.model.load_record(record)

    def start(self) -> dict[str, str]:
        failures: dict[str, str] = {}
        for record in self.registry.load():
            if record["adapterKind"] != "collab":
                continue
            link = self.credentials.get_link(record["id"])
            if link is None:
                self.model.set_transport(
                    record["id"],
                    instance_id=None,
                    generation=record["generation"],
                    status="disconnected",
                    reason="Collab credential removed",
                )
                continue
            try:
                self._connect_collab(record, link)
            except Exception:
                failures[record["id"]] = "collaboration connection unavailable"
                self.model.set_transport(
                    record["id"],
                    instance_id=None,
                    generation=record["generation"],
                    status="disconnected",
                    reason="Collaboration connection unavailable",
                )
        with self._lock:
            if self._watcher is None:
                self._stop.clear()
                self._watcher = threading.Thread(
                    target=self._watch_extensions,
                    name="omp-deck-extension-discovery",
                    daemon=True,
                )
                self._watcher.start()
        return failures

    def close(self, timeout: float = 5.0) -> None:
        self._stop.set()
        with self._lock:
            watcher = self._watcher
            self._watcher = None
            runtimes = self._runtimes.values()
        close_terminal_manager(
            self,
            runtimes,
            watcher,
            timeout,
            self._commands.lock,
        )

    def add_collab_request(
        self,
        request_id: str,
        link: str,
        *,
        title: str | None = None,
    ) -> dict[str, Any]:
        return self._collab_imports.dispatch(
            request_id,
            link,
            title=title,
        )

    def add_collab(
        self,
        link: str,
        *,
        title: str | None = None,
        guest_name: str = "OMP Deck",
    ) -> dict[str, Any]:
        return self._collab_imports.add(
            link,
            title=title,
            guest_name=guest_name,
        )

    def _rollback_collab(self, session_id: str) -> list[str]:
        errors = []
        runtime = self._runtimes.get(session_id)
        if runtime is not None:
            try:
                runtime.adapter.close()
            except Exception as error:
                return [f"transport: {error}"]
            self._runtimes.remove_if_current(runtime)
        try:
            self.credentials.remove(session_id)
        except Exception as error:
            errors.append(f"credential: {error}")
        try:
            if not self.registry.remove(session_id):
                errors.append("registry record disappeared")
        except Exception as error:
            errors.append(f"registry: {error}")
        try:
            self.model.remove(session_id)
        except Exception as error:
            errors.append(f"model: {error}")
        return errors

    def remove(self, session_id: str) -> bool:
        with self._commands.lock(session_id):
            try:
                record = _registry_record(self.registry, session_id)
            except KeyError:
                return False
            if record["adapterKind"] not in {"collab", "extension"}:
                return False
            runtime = self._runtimes.get(session_id)
            ignored_connection = (
                runtime.instance_id
                if runtime is not None and runtime.kind == "extension"
                else record.get("recoveryMetadata", {}).get("connectionId")
            )
            if runtime is not None:
                runtime.adapter.close()
            if record["adapterKind"] == "extension" and isinstance(ignored_connection, str):
                self.registry.update(
                    session_id,
                    recovery_metadata={
                        "connectionId": ignored_connection,
                        "removed": True,
                    },
                )
            else:
                if record["adapterKind"] == "collab":
                    self.credentials.remove(session_id)
                if not self.registry.remove(session_id):
                    return False
            if runtime is not None:
                self._runtimes.remove_if_current(runtime)
            with self._lock:
                if isinstance(ignored_connection, str):
                    self._ignored_extensions[session_id] = ignored_connection
                self._commands.forget(session_id)
            self.model.remove(session_id)
            return True

    def refresh_connected(self) -> None:
        runtimes = self._runtimes.values()
        for runtime in runtimes:
            try:
                if runtime.kind == "collab":
                    self.model.apply_collab_snapshot(
                        runtime.session_id,
                        instance_id=runtime.instance_id,
                        generation=runtime.generation,
                        snapshot=runtime.adapter.snapshot(),
                    )
                else:
                    registration = runtime.adapter.registration
                    snapshot = runtime.adapter.snapshot()
                    if registration is not None and snapshot is not None:
                        self.model.apply_extension_snapshot(
                            runtime.session_id,
                            instance_id=runtime.instance_id,
                            generation=runtime.generation,
                            registration=registration,
                            snapshot=snapshot,
                        )
            except Exception:
                continue

    def command(
        self,
        session_id: str,
        request_id: str,
        action: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        return self._commands.dispatch(
            session_id,
            request_id,
            action,
            payload,
            self._runtimes.require,
        )

    def _connect_collab(self, record: Mapping[str, Any], link: str, *, guest_name: str = "OMP Deck") -> None:
        session_id = str(record["id"])
        instance_id = uuid.uuid4().hex
        generation = int(record["generation"])

        def on_event(event: AdapterEvent) -> None:
            runtime = self._runtimes.get(session_id)
            if runtime is None or runtime.instance_id != instance_id:
                return
            if event.type in {"entry", "state", "agents", "ui_request", "ui_request_end", "transport"}:
                self.model.apply_collab_snapshot(
                    session_id,
                    instance_id=instance_id,
                    generation=runtime.generation,
                    snapshot=runtime.adapter.snapshot(),
                )
            else:
                self.model.apply_adapter_event(
                    session_id,
                    instance_id=instance_id,
                    generation=runtime.generation,
                    event=event,
                )

        adapter = CollabAdapter(link, guest_name, on_event=on_event)
        runtime = _Runtime(session_id, instance_id, generation, "collab", adapter)
        previous = self._runtimes.install(runtime)
        if previous is not None:
            previous.adapter.close()
        self.model.set_transport(
            session_id,
            instance_id=instance_id,
            generation=generation,
            status="connecting",
            reason=None,
        )
        try:
            adapter.start()
            if not self.model.apply_collab_snapshot(
                session_id,
                instance_id=instance_id,
                generation=generation,
                snapshot=adapter.snapshot(),
            ):
                raise TerminalSessionNotConnected(session_id)
        except Exception as error:
            try:
                adapter.close()
            except Exception as close_error:
                raise RuntimeError(f"{error}; Collab cleanup failed: {close_error}") from error
            self._runtimes.remove_if_current(runtime)
            raise

    def _watch_extensions(self) -> None:
        while not self._stop.is_set():
            try:
                candidates = discover_extension_candidates(
                    self.settings.socket_root,
                    open_file=os.open,
                    effective_uid=os.geteuid(),
                )
                self._reconcile_extensions(candidates)
            except Exception:
                pass
            self._stop.wait(self.discovery_interval)

    def _reconcile_extensions(self, candidates: Mapping[str, tuple[Path, dict[str, Any]]]) -> None:
        active_paths: set[Path] = set()
        for omp_session_id, (socket_path, metadata) in candidates.items():
            session_id = _terminal_id(omp_session_id)
            connection_id = metadata.get("connectionId")
            with self._lock:
                ignored = self._ignored_extensions.get(session_id)
                if ignored == connection_id:
                    continue
                if ignored is not None:
                    self._ignored_extensions.pop(session_id, None)
            active_paths.add(socket_path)
            runtime = self._runtimes.get(session_id)
            if runtime is not None and runtime.kind == "extension" and runtime.socket_path == socket_path:
                snapshot = runtime.adapter.snapshot()
                if snapshot is not None and snapshot.transport_status == "connected":
                    continue
                self._drop_runtime(runtime, "Extension heartbeat expired")
            try:
                self._connect_extension(session_id, socket_path, metadata)
            except Exception:
                continue
        stale = [
            runtime
            for runtime in self._runtimes.values()
            if runtime.kind == "extension" and runtime.socket_path not in active_paths
        ]
        for runtime in stale:
            self._drop_runtime(runtime, "Extension registration expired")

    def _connect_extension(
        self,
        session_id: str,
        socket_path: Path,
        metadata: Mapping[str, Any],
    ) -> None:
        with self._commands.lock(session_id):
            self._connect_extension_locked(session_id, socket_path, metadata)

    def _connect_extension_locked(
        self,
        session_id: str,
        socket_path: Path,
        metadata: Mapping[str, Any],
    ) -> None:
        connection_id = metadata.get("connectionId")
        if not isinstance(connection_id, str):
            return
        with self._lock:
            if self._stop.is_set() or self._ignored_extensions.get(session_id) == connection_id:
                return
        title = (
            metadata.get("title")
            if isinstance(metadata.get("title"), str) and metadata.get("title")
            else "Terminal session"
        )
        project_path = (
            metadata.get("project") if isinstance(metadata.get("project"), str) else str(self.settings.project_root)
        )
        project_name = Path(project_path).name or "Terminal"
        try:
            record = _registry_record(self.registry, session_id)
            record = self.registry.update(
                session_id,
                adapter_kind="extension",
                omp_session_id=str(metadata["sessionId"]),
                project_name=project_name,
                project_path=project_path,
                title=title,
                recovery_metadata={"connectionId": connection_id},
            )
        except KeyError:
            record = self.registry.create(
                session_id=session_id,
                adapter_kind="extension",
                omp_session_id=str(metadata["sessionId"]),
                project_name=project_name,
                project_path=project_path,
                title=title,
                recovery_metadata={"connectionId": connection_id},
            )
        self.model.load_record(record)
        generation = int(record["generation"])

        def on_event(event: Mapping[str, Any]) -> None:
            if event.get("kind") == "snapshot":
                return
            runtime = self._runtimes.get(session_id)
            if runtime is not None and runtime.instance_id == connection_id:
                self.model.apply_extension_event(
                    session_id,
                    instance_id=connection_id,
                    generation=runtime.generation,
                    event=event,
                )

        adapter = ExtensionAdapter(str(socket_path), on_event=on_event)
        runtime = _Runtime(session_id, connection_id, generation, "extension", adapter, socket_path)
        with self._lock:
            if self._stop.is_set() or self._ignored_extensions.get(session_id) == connection_id:
                return
            previous = self._runtimes.install(runtime)
        if previous is not None:
            previous.adapter.close()
        self.model.set_transport(
            session_id,
            instance_id=connection_id,
            generation=generation,
            status="connecting",
            reason=None,
        )
        try:
            registration = adapter.start()
            with self._lock:
                if (
                    self._stop.is_set()
                    or not self._runtimes.is_current(runtime)
                    or self._ignored_extensions.get(session_id) == connection_id
                ):
                    raise TerminalSessionNotConnected(session_id)
            snapshot = adapter.snapshot()
            if snapshot is None or not self.model.apply_extension_snapshot(
                session_id,
                instance_id=connection_id,
                generation=generation,
                registration=registration,
                snapshot=snapshot,
            ):
                raise TerminalSessionNotConnected(session_id)
        except Exception as error:
            try:
                adapter.close()
            except Exception as close_error:
                self.model.set_transport(
                    session_id,
                    instance_id=connection_id,
                    generation=generation,
                    status="degraded",
                    reason=f"Extension cleanup failed: {close_error}",
                )
                raise RuntimeError(f"{error}; Extension cleanup failed: {close_error}") from error
            self._runtimes.remove_if_current(runtime)
            self.model.set_transport(
                session_id,
                instance_id=None,
                generation=generation,
                status="disconnected",
                reason="Extension connection unavailable",
            )
            raise

    def _drop_runtime(self, runtime: _Runtime, reason: str) -> None:
        with self._commands.lock(runtime.session_id):
            if not self._runtimes.is_current(runtime):
                return
            runtime.adapter.close()
            self._runtimes.remove_if_current(runtime)
            self.model.set_transport(
                runtime.session_id,
                instance_id=None,
                generation=runtime.generation,
                status="disconnected",
                reason=reason,
            )
