"""Ownership and supervision for terminal-owned OMP transports."""
from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ._terminal_discovery import discover_extension_candidates, terminal_id as _terminal_id
from .collab_adapter import CollabAdapter
from .collab_protocol import parse_collab_link
from .collab_store import CollabStore
from .extension_adapter import ExtensionAdapter
from .fleet_model import FleetModel
from .registry_store import RegistryStore
from .rpc_adapter import AdapterEvent
from .settings import Settings

_DISCOVERY_INTERVAL = 2.0


class TerminalSessionError(Exception):
    pass


class TerminalSessionNotConnected(TerminalSessionError, LookupError):
    pass


class TerminalCommandConflict(TerminalSessionError, ValueError):
    pass


@dataclass(slots=True)
class _Runtime:
    session_id: str
    instance_id: str
    generation: int
    kind: str
    adapter: CollabAdapter | ExtensionAdapter
    socket_path: Path | None = None


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value




class TerminalSessionManager:
    """Connects registered extension sockets and explicit Collab credentials."""

    def __init__(
        self,
        settings: Settings,
        model: FleetModel,
        *,
        registry: RegistryStore | None = None,
        credentials: CollabStore | None = None,
        discovery_interval: float = _DISCOVERY_INTERVAL,
    ) -> None:
        if discovery_interval <= 0:
            raise ValueError("discovery_interval must be positive")
        self.settings = settings
        self.model = model
        self.registry = registry or RegistryStore(settings.state_root / "registry.json")
        self.credentials = credentials or CollabStore(settings.state_root)
        self.discovery_interval = discovery_interval
        self._lock = threading.RLock()
        self._runtimes: dict[str, _Runtime] = {}
        self._command_locks: dict[str, threading.Lock] = {}
        self._results: OrderedDict[tuple[str, str], tuple[str, dict[str, Any]]] = OrderedDict()
        self._result_limit = 1_000
        self._stop = threading.Event()
        self._watcher: threading.Thread | None = None
        self._ignored_extensions: dict[str, str] = {}
        for record in self.registry.load():
            if record["adapterKind"] in {"collab", "extension"}:
                self.model.load_record(record)

    def start(self) -> dict[str, str]:
        failures: dict[str, str] = {}
        for record in self.registry.load():
            if record["adapterKind"] != "collab":
                continue
            link = self.credentials.get_link(record["id"])
            if link is None:
                self.model.set_transport(
                    record["id"], instance_id=None, generation=record["generation"],
                    status="disconnected", reason="Collab credential removed",
                )
                continue
            try:
                self._connect_collab(record, link)
            except Exception:
                failures[record["id"]] = "collaboration connection unavailable"
                self.model.set_transport(
                    record["id"], instance_id=None, generation=record["generation"],
                    status="disconnected", reason="Collaboration connection unavailable",
                )
        with self._lock:
            if self._watcher is None:
                self._stop.clear()
                self._watcher = threading.Thread(
                    target=self._watch_extensions,
                    name="omp-deck-extension-discovery",
                    daemon=False,
                )
                self._watcher.start()
        return failures

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            watcher = self._watcher
            self._watcher = None
        if watcher is not None and watcher is not threading.current_thread():
            watcher.join(max(5.0, self.discovery_interval * 2))
        with self._lock:
            runtimes = tuple(self._runtimes.values())
            self._runtimes.clear()
        for runtime in runtimes:
            runtime.adapter.close()
            self.model.set_transport(
                runtime.session_id,
                instance_id=None,
                generation=runtime.generation,
                status="disconnected",
                reason="service stopped",
            )

    def add_collab(self, link: str, *, title: str | None = None, guest_name: str = "OMP Deck") -> dict[str, Any]:
        parse_collab_link(link)
        record = self.registry.create(
            adapter_kind="collab",
            project_name="Terminal",
            project_path=str(self.settings.project_root),
            title=title or "Terminal session",
        )
        self.model.load_record(record)
        self.credentials.put(record["id"], link)
        try:
            self._connect_collab(record, link, guest_name=guest_name)
            adapter = self._runtime(record["id"]).adapter
            snapshot = adapter.snapshot()
            header = snapshot.header
            state = snapshot.state
            project_path = state.get("cwd") if isinstance(state.get("cwd"), str) else str(self.settings.project_root)
            project_name = Path(project_path).name or "Terminal"
            updated = self.registry.update(
                record["id"],
                omp_session_id=header.get("id") if isinstance(header.get("id"), str) else None,
                project_name=project_name,
                project_path=project_path,
                title=(header.get("title") if isinstance(header.get("title"), str) and header.get("title") else title or project_name),
            )
            self.model.load_record(updated)
            runtime = self._runtime(record["id"])
            runtime.generation = updated["generation"]
            self.model.apply_collab_snapshot(
                record["id"], instance_id=runtime.instance_id,
                generation=runtime.generation, snapshot=snapshot,
            )
        except Exception:
            current = self._record(record["id"])
            self.model.set_transport(
                record["id"], instance_id=None, generation=current["generation"],
                status="disconnected", reason="Collaboration connection unavailable",
            )
            raise TerminalSessionError("collaboration connection unavailable") from None
        return self.model.get(record["id"])

    def remove(self, session_id: str) -> bool:
        with self._lock:
            runtime = self._runtimes.pop(session_id, None)
            if runtime is not None and runtime.kind == "extension":
                self._ignored_extensions[session_id] = runtime.instance_id
            self._command_locks.pop(session_id, None)
            for key in tuple(self._results):
                if key[0] == session_id:
                    self._results.pop(key, None)
        if runtime is not None:
            runtime.adapter.close()
        self.credentials.remove(session_id)
        removed = self.registry.remove(session_id)
        if removed:
            self.model.remove(session_id)
        return removed

    def refresh_connected(self) -> None:
        with self._lock:
            runtimes = tuple(self._runtimes.values())
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

    def command(self, session_id: str, request_id: str, action: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        capability = {
            "sendMessage": "sendMessage",
            "followUp": "followUp",
            "interrupt": "interrupt",
            "shutdown": "shutdown",
            "respondToUi": "answerUiRequest",
            "cancelUi": "cancelUiRequest",
        }.get(action)
        if capability is None or not self.model.supports(session_id, capability):
            raise ValueError("action is unavailable for this terminal transport")
        runtime = self._runtime(session_id)
        fingerprint = hashlib.sha256(json.dumps(
            {"action": action, "payload": _plain(payload)}, sort_keys=True,
            ensure_ascii=False, allow_nan=False, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        key = (session_id, request_id)
        with self._lock:
            previous = self._results.get(key)
            if previous is not None:
                if previous[0] != fingerprint:
                    raise TerminalCommandConflict("requestId was already used for another command")
                return dict(previous[1])
            command_lock = self._command_locks.setdefault(session_id, threading.Lock())
        with command_lock:
            with self._lock:
                previous = self._results.get(key)
                if previous is not None:
                    if previous[0] != fingerprint:
                        raise TerminalCommandConflict("requestId was already used for another command")
                    return dict(previous[1])
            current = self._runtime(session_id)
            if current is not runtime:
                raise TerminalSessionNotConnected(session_id)
            adapter = runtime.adapter
            if action == "sendMessage":
                if runtime.kind == "collab":
                    adapter.send_message(payload["message"], images=payload.get("images"))
                else:
                    adapter.send_message(payload["message"])
            elif action == "followUp" and runtime.kind == "extension":
                adapter.send_message(payload["message"], "followUp")
            elif action == "interrupt":
                adapter.interrupt()
            elif action == "shutdown" and runtime.kind == "extension":
                adapter.shutdown_session()
            elif action in {"respondToUi", "cancelUi"} and runtime.kind == "collab":
                try:
                    numeric_id = int(payload["requestId"])
                except (TypeError, ValueError):
                    raise ValueError("invalid Collab request id") from None
                value = payload.get("value") if action == "respondToUi" else None
                if value is not None and not isinstance(value, str):
                    value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                if not adapter.respond_ui(numeric_id, value):
                    raise ValueError("UI request is stale")
            else:
                raise ValueError("action is unavailable for this terminal transport")
            result = {"requestId": request_id, "status": "accepted"}
            with self._lock:
                self._results[key] = (fingerprint, result)
                self._results.move_to_end(key)
                while len(self._results) > self._result_limit:
                    self._results.popitem(last=False)
            return dict(result)

    def _connect_collab(self, record: Mapping[str, Any], link: str, *, guest_name: str = "OMP Deck") -> None:
        session_id = str(record["id"])
        instance_id = uuid.uuid4().hex
        generation = int(record["generation"])

        def on_event(event: AdapterEvent) -> None:
            runtime = self._runtime_or_none(session_id)
            if runtime is None or runtime.instance_id != instance_id:
                return
            if event.type in {"entry", "state", "agents", "ui_request", "ui_request_end", "transport"}:
                self.model.apply_collab_snapshot(
                    session_id, instance_id=instance_id,
                    generation=runtime.generation, snapshot=runtime.adapter.snapshot(),
                )
            else:
                self.model.apply_adapter_event(
                    session_id, instance_id=instance_id,
                    generation=runtime.generation, event=event,
                )

        adapter = CollabAdapter(link, guest_name, on_event=on_event)
        runtime = _Runtime(session_id, instance_id, generation, "collab", adapter)
        with self._lock:
            previous = self._runtimes.get(session_id)
            self._runtimes[session_id] = runtime
        if previous is not None:
            previous.adapter.close()
        self.model.set_transport(
            session_id, instance_id=instance_id, generation=generation,
            status="connecting", reason=None,
        )
        try:
            adapter.start()
            if not self.model.apply_collab_snapshot(
                session_id, instance_id=instance_id, generation=generation,
                snapshot=adapter.snapshot(),
            ):
                raise TerminalSessionNotConnected(session_id)
        except Exception:
            with self._lock:
                if self._runtimes.get(session_id) is runtime:
                    self._runtimes.pop(session_id, None)
            adapter.close()
            raise

    def _watch_extensions(self) -> None:
        while not self._stop.is_set():
            try:
                candidates = self._extension_candidates()
                self._reconcile_extensions(candidates)
            except Exception:
                pass
            self._stop.wait(self.discovery_interval)

    def _extension_candidates(self) -> dict[str, tuple[Path, dict[str, Any]]]:
        return discover_extension_candidates(
            self.settings.socket_root,
            open_file=os.open,
            effective_uid=os.geteuid(),
        )

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
            runtime = self._runtime_or_none(session_id)
            if runtime is not None and runtime.kind == "extension" and runtime.socket_path == socket_path:
                snapshot = runtime.adapter.snapshot()
                if snapshot is not None and snapshot.transport_status == "connected":
                    continue
                self._drop_runtime(runtime, "Extension heartbeat expired")
            try:
                self._connect_extension(session_id, socket_path, metadata)
            except Exception:
                continue
        with self._lock:
            stale = [runtime for runtime in self._runtimes.values() if runtime.kind == "extension" and runtime.socket_path not in active_paths]
        for runtime in stale:
            self._drop_runtime(runtime, "Extension registration expired")

    def _connect_extension(self, session_id: str, socket_path: Path, metadata: Mapping[str, Any]) -> None:
        connection_id = metadata.get("connectionId")
        if not isinstance(connection_id, str):
            return
        with self._lock:
            if self._stop.is_set() or self._ignored_extensions.get(session_id) == connection_id:
                return
        title = metadata.get("title") if isinstance(metadata.get("title"), str) and metadata.get("title") else "Terminal session"
        project_path = metadata.get("project") if isinstance(metadata.get("project"), str) else str(self.settings.project_root)
        project_name = Path(project_path).name or "Terminal"
        try:
            record = self._record(session_id)
            record = self.registry.update(
                session_id, adapter_kind="extension", omp_session_id=str(metadata["sessionId"]),
                project_name=project_name, project_path=project_path, title=title,
            )
        except KeyError:
            record = self.registry.create(
                session_id=session_id, adapter_kind="extension",
                omp_session_id=str(metadata["sessionId"]), project_name=project_name,
                project_path=project_path, title=title,
            )
        self.model.load_record(record)
        generation = int(record["generation"])

        def on_event(event: Mapping[str, Any]) -> None:
            if event.get("kind") == "snapshot":
                return
            runtime = self._runtime_or_none(session_id)
            if runtime is not None and runtime.instance_id == connection_id:
                self.model.apply_extension_event(
                    session_id, instance_id=connection_id,
                    generation=runtime.generation, event=event,
                )

        adapter = ExtensionAdapter(str(socket_path), on_event=on_event)
        runtime = _Runtime(session_id, connection_id, generation, "extension", adapter, socket_path)
        with self._lock:
            if self._stop.is_set() or self._ignored_extensions.get(session_id) == connection_id:
                return
            previous = self._runtimes.get(session_id)
            self._runtimes[session_id] = runtime
        if previous is not None:
            previous.adapter.close()
        self.model.set_transport(
            session_id, instance_id=connection_id, generation=generation,
            status="connecting", reason=None,
        )
        try:
            registration = adapter.start()
            with self._lock:
                if (
                    self._stop.is_set()
                    or self._runtimes.get(session_id) is not runtime
                    or self._ignored_extensions.get(session_id) == connection_id
                ):
                    raise TerminalSessionNotConnected(session_id)
            snapshot = adapter.snapshot()
            if snapshot is None or not self.model.apply_extension_snapshot(
                session_id, instance_id=connection_id, generation=generation,
                registration=registration, snapshot=snapshot,
            ):
                raise TerminalSessionNotConnected(session_id)
        except Exception:
            with self._lock:
                if self._runtimes.get(session_id) is runtime:
                    self._runtimes.pop(session_id, None)
            adapter.close()
            self.model.set_transport(
                session_id, instance_id=None, generation=generation,
                status="disconnected", reason="Extension connection unavailable",
            )
            raise

    def _drop_runtime(self, runtime: _Runtime, reason: str) -> None:
        with self._lock:
            if self._runtimes.get(runtime.session_id) is not runtime:
                return
            self._runtimes.pop(runtime.session_id, None)
        runtime.adapter.close()
        self.model.set_transport(
            runtime.session_id, instance_id=None, generation=runtime.generation,
            status="disconnected", reason=reason,
        )

    def _record(self, session_id: str) -> dict[str, Any]:
        for record in self.registry.load():
            if record["id"] == session_id:
                return record
        raise KeyError(session_id)

    def _runtime_or_none(self, session_id: str) -> _Runtime | None:
        with self._lock:
            return self._runtimes.get(session_id)

    def _runtime(self, session_id: str) -> _Runtime:
        runtime = self._runtime_or_none(session_id)
        if runtime is None:
            raise TerminalSessionNotConnected(session_id)
        return runtime
