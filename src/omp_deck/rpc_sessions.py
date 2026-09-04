from __future__ import annotations

import dataclasses
import hashlib
import json
import threading
import uuid
from enum import Enum
from collections import OrderedDict
from pathlib import Path
from typing import Any, Mapping

from omp_rpc import RpcClient

from .fleet_model import FleetModel
from .projects import resolve_project_path
from .registry_store import RegistryStore
from .rpc_adapter import (
    AdapterDisconnectedError,
    AdapterError,
    AdapterEvent,
    AdapterSnapshot,
    SessionAdapter,
)
from .settings import Settings


class SessionNotConnectedError(LookupError):
    pass
class SessionCommandConflictError(ValueError):
    pass




def _plain(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return {field.name: _plain(getattr(value, field.name)) for field in dataclasses.fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


@dataclasses.dataclass(slots=True)
class _Runtime:
    session_id: str
    instance_id: str
    generation: int
    adapter: SessionAdapter | None = None


class RpcSessionManager:
    """Owns Deck-launched RPC processes without making process state fleet state."""

    def __init__(self, settings: Settings, model: FleetModel, registry: RegistryStore | None = None) -> None:
        self.settings = settings
        self.model = model
        self.registry = registry or RegistryStore(settings.state_root / "registry.json")
        self._lock = threading.RLock()
        self._runtimes: dict[str, _Runtime] = {}
        self._command_locks: dict[str, threading.Lock] = {}
        self._command_results: OrderedDict[tuple[str, str], tuple[str, dict[str, Any]]] = OrderedDict()
        self._command_result_limit = 1_000
        self._launch_locks: dict[str, threading.Lock] = {}
        self._launch_results: OrderedDict[str, tuple[str, dict[str, Any]]] = OrderedDict()
        for record in self.registry.load():
            self.model.load_record(record)

    def recover_desired(self) -> dict[str, str]:
        failures: dict[str, str] = {}
        for record in self.registry.load():
            if record["adapterKind"] != "rpc" or not record["desiredRpcRunning"]:
                continue
            if self._runtime_or_none(record["id"]) is not None:
                continue
            try:
                self._connect(record, resume=record.get("ompSessionId"))
            except Exception as error:
                failures[record["id"]] = str(error)
                current = self._record(record["id"])
                self.model.set_transport(
                    record["id"],
                    instance_id=None,
                    generation=current["generation"],
                    status="disconnected",
                    reason=f"recovery failed: {error}",
                )
        return failures

    def launch(self, project_name: str, project_path: Path, *, title: str | None = None) -> dict[str, Any]:
        record = self.registry.create(
            adapter_kind="rpc",
            project_name=project_name,
            project_path=str(project_path),
            title=title or project_name,
            desired_rpc_running=True,
        )
        self.model.load_record(record)
        try:
            self._connect(record, resume=None)
        except Exception as error:
            failed = self.registry.update(record["id"], desired_rpc_running=False)
            self.model.load_record(failed)
            self.model.set_transport(
                record["id"],
                instance_id=None,
                generation=failed["generation"],
                status="disconnected",
                reason=f"launch failed: {error}",
            )
            raise
        return self.model.get(record["id"])

    def launch_request(
        self,
        request_id: str,
        project_name: str,
        project_path: Path,
        *,
        title: str | None = None,
    ) -> dict[str, Any]:
        fingerprint = hashlib.sha256(
            json.dumps(
                {"project": project_name, "path": str(project_path), "title": title},
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        with self._lock:
            previous = self._launch_results.get(request_id)
            if previous is not None:
                if previous[0] != fingerprint:
                    raise SessionCommandConflictError("requestId was already used for another launch")
                return dict(previous[1])
            launch_lock = self._launch_locks.setdefault(project_name, threading.Lock())
        with launch_lock:
            with self._lock:
                previous = self._launch_results.get(request_id)
                if previous is not None:
                    if previous[0] != fingerprint:
                        raise SessionCommandConflictError("requestId was already used for another launch")
                    return dict(previous[1])
            try:
                session = self.launch(project_name, project_path, title=title)
                result = {"requestId": request_id, "status": "accepted", "session": session}
            except Exception:
                result = {"requestId": request_id, "status": "failed", "errorCode": "launch_failed"}
            with self._lock:
                self._launch_results[request_id] = (fingerprint, result)
                self._launch_results.move_to_end(request_id)
                while len(self._launch_results) > self._command_result_limit:
                    self._launch_results.popitem(last=False)
            return dict(result)

    def refresh(self, session_id: str) -> dict[str, Any]:
        runtime = self._runtime(session_id)
        adapter = runtime.adapter
        if adapter is None:
            raise SessionNotConnectedError(session_id)
        snapshot = adapter.snapshot()
        if not self.model.apply_rpc_snapshot(
            session_id,
            instance_id=runtime.instance_id,
            generation=runtime.generation,
            snapshot=snapshot,
        ):
            raise SessionNotConnectedError(session_id)
        return self.model.get(session_id)

    def refresh_connected(self) -> None:
        with self._lock:
            session_ids = tuple(self._runtimes)
        for session_id in session_ids:
            try:
                self.refresh(session_id)
            except AdapterDisconnectedError as error:
                self._replace_disconnected(session_id, error)
    def dispatch(
        self,
        session_id: str,
        request_id: str,
        action: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        fingerprint = hashlib.sha256(
            json.dumps(
                {"action": action, "payload": payload},
                sort_keys=True,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        key = (session_id, request_id)
        with self._lock:
            previous = self._command_results.get(key)
            if previous is not None:
                if previous[0] != fingerprint:
                    raise SessionCommandConflictError("requestId was already used for another command")
                return dict(previous[1])
            command_lock = self._command_locks.setdefault(session_id, threading.Lock())
        with command_lock:
            with self._lock:
                previous = self._command_results.get(key)
                if previous is not None:
                    if previous[0] != fingerprint:
                        raise SessionCommandConflictError("requestId was already used for another command")
                    return dict(previous[1])
            try:
                command_result = self.command(session_id, action, payload)
                outcome = command_result.get("result")
                status = "stale" if outcome == "stale" else "accepted"
                result = {
                    "requestId": request_id,
                    "status": status,
                    "result": outcome,
                }
            except AdapterError as error:
                status = "cancelled" if error.kind == "cancelled" else "failed"
                result = {
                    "requestId": request_id,
                    "status": status,
                    "errorCode": error.kind,
                }
            with self._lock:
                self._command_results[key] = (fingerprint, result)
                self._command_results.move_to_end(key)
                while len(self._command_results) > self._command_result_limit:
                    self._command_results.popitem(last=False)
            return dict(result)

    def command(self, session_id: str, action: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        runtime = self._runtime(session_id)
        adapter = runtime.adapter
        if adapter is None:
            raise SessionNotConnectedError(session_id)
        result: Any = None
        if action == "sendMessage":
            adapter.send_message(
                payload["message"],
                images=payload.get("images"),
                streaming_behavior=payload.get("streamingBehavior"),
            )
        elif action == "followUp":
            adapter.follow_up(payload["message"], images=payload.get("images"))
        elif action == "interrupt":
            adapter.interrupt()
        elif action == "setModel":
            result = adapter.set_model(payload["provider"], payload["modelId"])
        elif action == "setThinking":
            adapter.set_thinking(payload["level"])
        elif action == "branch":
            result = adapter.branch(payload["entryId"])
        elif action == "switchSession":
            result = adapter.switch(payload["sessionPath"])
        elif action == "compact":
            result = adapter.compact(payload.get("customInstructions"))
        elif action == "respondToUi":
            result = adapter.respond_ui(payload["requestId"], payload["value"])
        elif action == "cancelUi":
            result = adapter.cancel_ui(payload["requestId"])
        elif action == "shutdown":
            self.shutdown(session_id)
            return {"accepted": True, "result": None}
        else:
            raise ValueError(f"unsupported RPC action: {action}")
        snapshot = adapter.snapshot()
        if action in {"branch", "switchSession"}:
            final = self._persist_rpc_identity(self._record(session_id), snapshot)
            runtime.generation = final["generation"]
            self.model.load_record(final)
        self.model.apply_rpc_snapshot(
            session_id,
            instance_id=runtime.instance_id,
            generation=runtime.generation,
            snapshot=snapshot,
        )
        return {"accepted": True, "result": _plain(result)}

    def shutdown(self, session_id: str) -> None:
        with self._lock:
            runtime = self._runtimes.pop(session_id, None)
        record = self.registry.update(session_id, desired_rpc_running=False)
        if runtime is not None:
            runtime.generation = record["generation"]
        self.model.load_record(record)
        if runtime is not None and runtime.adapter is not None:
            runtime.adapter.shutdown()
        self.model.set_transport(
            session_id,
            instance_id=runtime.instance_id if runtime else None,
            generation=record["generation"],
            status="disconnected",
            reason="shut down",
        )

    def close(self) -> None:
        with self._lock:
            runtimes = tuple(self._runtimes.values())
            self._runtimes.clear()
        for runtime in runtimes:
            if runtime.adapter is not None:
                try:
                    runtime.adapter.close()
                except Exception as error:
                    self.model.set_transport(
                        runtime.session_id,
                        instance_id=runtime.instance_id,
                        generation=runtime.generation,
                        status="disconnected",
                        reason=f"close failed: {error}",
                    )

    def _connect(self, record: Mapping[str, Any], *, resume: str | None) -> None:
        runtime = _Runtime(
            session_id=record["id"],
            instance_id=uuid.uuid4().hex,
            generation=record["generation"],
        )
        with self._lock:
            if runtime.session_id in self._runtimes:
                raise RuntimeError(f"session {runtime.session_id} is already connected")
            self._runtimes[runtime.session_id] = runtime
        adapter = None
        try:
            project_path = self._validated_project(record)
            updated = self.registry.update(record["id"], desired_rpc_running=True)
            runtime.generation = updated["generation"]
            self.model.set_transport(
                runtime.session_id,
                instance_id=runtime.instance_id,
                generation=runtime.generation,
                status="connecting",
                reason=None,
            )
            self.model.load_record(updated)

            def client_factory() -> RpcClient:
                extra_args = ("--resume", resume) if resume else ()
                return RpcClient(
                    executable=self.settings.omp_bin,
                    cwd=project_path,
                    session_dir=self.settings.session_root,
                    extra_args=extra_args,
                    max_event_history=512,
                    max_stderr_chunks=128,
                )

            adapter = SessionAdapter(
                client_factory,
                on_event=lambda event: self._on_event(runtime, event),
                event_limit=512,
                ui_limit=128,
            )
            runtime.adapter = adapter
            adapter.start()
            snapshot = adapter.snapshot()
            final = self._persist_rpc_identity(updated, snapshot)
            runtime.generation = final["generation"]
            self.model.load_record(final)
            self.model.apply_rpc_snapshot(
                runtime.session_id,
                instance_id=runtime.instance_id,
                generation=runtime.generation,
                snapshot=snapshot,
            )
        except Exception:
            with self._lock:
                if self._runtimes.get(runtime.session_id) is runtime:
                    self._runtimes.pop(runtime.session_id)
            if adapter is not None:
                try:
                    adapter.close()
                except Exception:
                    pass
            raise

    def _persist_rpc_identity(self, record: Mapping[str, Any], snapshot: AdapterSnapshot) -> dict[str, Any]:
        state = snapshot.state
        recovery: dict[str, Any] = {}
        session_file = state.get("session_file")
        if isinstance(session_file, str) and session_file:
            recovery["sessionFile"] = session_file
        omp_session_id = state.get("session_id")
        return self.registry.update(
            record["id"],
            omp_session_id=omp_session_id if isinstance(omp_session_id, str) and omp_session_id else record.get("ompSessionId"),
            recovery_metadata=recovery,
        )
    def _validated_project(self, record: Mapping[str, Any]) -> str:
        project = record["project"]
        resolved = resolve_project_path(
            self.settings.project_root, project["name"], must_exist=True
        )
        if str(resolved) != project["path"] or not resolved.is_dir():
            raise ValueError("registered project path is no longer valid")
        return str(resolved)

    def _record(self, session_id: str) -> dict[str, Any]:
        for record in self.registry.load():
            if record["id"] == session_id:
                return record
        raise KeyError(session_id)

    def _replace_disconnected(self, session_id: str, error: Exception) -> None:
        with self._lock:
            runtime = self._runtimes.pop(session_id, None)
        if runtime is None:
            return
        if runtime.adapter is not None:
            try:
                runtime.adapter.close()
            except Exception:
                pass
        self.model.set_transport(
            session_id,
            instance_id=runtime.instance_id,
            generation=runtime.generation,
            status="disconnected",
            reason=str(error),
        )
        record = self._record(session_id)
        if not record["desiredRpcRunning"]:
            return
        try:
            self._connect(record, resume=record.get("ompSessionId"))
        except Exception as reconnect_error:
            current = self._record(session_id)
            self.model.set_transport(
                session_id,
                instance_id=None,
                generation=current["generation"],
                status="disconnected",
                reason=f"reconnect failed: {reconnect_error}",
            )

    def _on_event(self, runtime: _Runtime, event: AdapterEvent) -> None:
        self.model.apply_adapter_event(
            runtime.session_id,
            instance_id=runtime.instance_id,
            generation=runtime.generation,
            event=event,
        )

    def _runtime(self, session_id: str) -> _Runtime:
        runtime = self._runtime_or_none(session_id)
        if runtime is None:
            raise SessionNotConnectedError(session_id)
        return runtime

    def _runtime_or_none(self, session_id: str) -> _Runtime | None:
        with self._lock:
            return self._runtimes.get(session_id)
