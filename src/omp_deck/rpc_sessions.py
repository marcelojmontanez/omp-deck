from __future__ import annotations

import dataclasses
import os
import sys
import threading
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from omp_rpc import RpcClient

from . import _rpc_commands
from ._rpc_recovery import recovery_session_file, validate_recovered_identity, validated_session_file
from ._shutdown import close_rpc_manager
from .fleet_model import FleetModel
from .projects import resolve_project_path
from .registry_store import RegistryStore
from .request_ledger import RequestConflictError, RequestLedger
from .rpc_adapter import (
    AdapterEvent,
    AdapterSnapshot,
    SessionAdapter,
)
from .rpc_refresh import RpcRefreshScheduler
from .rpc_requests import RpcRequestCoordinator
from .settings import Settings


class SessionNotConnectedError(LookupError):
    pass


class SessionCommandConflictError(ValueError):
    pass


@dataclasses.dataclass(slots=True)
class _Runtime:
    session_id: str
    instance_id: str
    generation: int
    adapter: SessionAdapter | None = None


class RpcSessionManager:
    """Owns Deck-launched RPC processes without making process state fleet state."""

    def __init__(
        self,
        settings: Settings,
        model: FleetModel,
        registry: RegistryStore | None = None,
        requests: RequestLedger | None = None,
    ) -> None:
        self.settings = settings
        self.model = model
        self.registry = registry or RegistryStore(settings.state_root / "registry.json")
        self.requests = requests or RequestLedger(settings.state_root / "requests.json")
        self._requests = RpcRequestCoordinator(settings, model, self.requests)
        self._lock = threading.RLock()
        self._runtimes: dict[str, _Runtime] = {}
        self._operation_locks: dict[str, threading.RLock] = {}
        self._closed = False
        self._refreshes = RpcRefreshScheduler(
            self._refresh_or_reconnect,
            self._replace_disconnected,
            self._mark_refresh_failed,
        )
        for record in self.registry.load():
            if record["adapterKind"] == "rpc":
                self.model.load_record(record)

    def recover_desired(self) -> dict[str, str]:
        failures: dict[str, str] = {}
        for record in self.registry.load():
            if record["adapterKind"] != "rpc" or not record["desiredRpcRunning"]:
                continue
            if self._runtime_or_none(record["id"]) is not None:
                continue
            try:
                self._connect(record)
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
            self._connect(record)
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
        *,
        title: str | None = None,
        create: bool = False,
    ) -> dict[str, Any]:
        try:
            return self._requests.launch(
                request_id,
                project_name,
                title=title,
                create=create,
                start=self.launch,
            )
        except RequestConflictError:
            raise SessionCommandConflictError("requestId was already used for another launch") from None

    def refresh(self, session_id: str) -> dict[str, Any]:
        runtime = self._runtime(session_id)
        adapter = runtime.adapter
        if adapter is None:
            raise SessionNotConnectedError(session_id)
        snapshot = adapter.snapshot()
        record = self._persist_rpc_identity(self._record(session_id), snapshot)
        runtime.generation = record["generation"]
        self.model.load_record(record)
        if not self.model.apply_rpc_snapshot(
            session_id,
            instance_id=runtime.instance_id,
            generation=runtime.generation,
            snapshot=snapshot,
        ):
            raise SessionNotConnectedError(session_id)
        return self.model.get(session_id)

    def refresh_connected(self) -> None:
        self._refreshes.refresh_now(self._desired_rpc_session_ids())

    def request_refresh_connected(self) -> None:
        self._refreshes.request(self._desired_rpc_session_ids())

    def _desired_rpc_session_ids(self) -> tuple[str, ...]:
        return tuple(
            record["id"]
            for record in self.registry.load()
            if record["adapterKind"] == "rpc" and record["desiredRpcRunning"]
        )

    def _refresh_or_reconnect(self, session_id: str) -> dict[str, Any]:
        if self._runtime_or_none(session_id) is not None:
            return self.refresh(session_id)
        record = self._record(session_id)
        if record["adapterKind"] != "rpc" or not record["desiredRpcRunning"]:
            raise SessionNotConnectedError(session_id)
        self._connect(record)
        return self.model.get(session_id)

    def dispatch(
        self,
        session_id: str,
        request_id: str,
        action: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        with self._operation_lock(session_id):
            try:
                return self._requests.dispatch(
                    session_id,
                    request_id,
                    action,
                    payload,
                    prepare=lambda: self._prepare_command(session_id, action, payload),
                    execute=self._command,
                )
            except RequestConflictError:
                raise SessionCommandConflictError("requestId was already used for another command") from None

    def command(self, session_id: str, action: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        with self._operation_lock(session_id):
            return self._command(self._connected_runtime(session_id), action, payload)

    def _command(self, runtime: _Runtime, action: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        session_id = runtime.session_id
        adapter = runtime.adapter
        if adapter is None:
            raise SessionNotConnectedError(session_id)
        if action == "shutdown":
            self.shutdown(session_id)
            return {"accepted": True, "result": None}
        result = _rpc_commands.invoke(adapter, action, payload)
        try:
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
        except Exception:
            self._refreshes.request((session_id,))
        return {"accepted": True, "result": _rpc_commands.plain(result)}

    def shutdown(self, session_id: str) -> None:
        with self._operation_lock(session_id):
            runtime = self._runtime_or_none(session_id)
            record = self.registry.update(session_id, desired_rpc_running=False)
            if runtime is not None:
                runtime.generation = record["generation"]
            self.model.load_record(record)
            try:
                if runtime is not None and runtime.adapter is not None:
                    runtime.adapter.shutdown()
            except Exception:
                self.model.set_transport(
                    session_id,
                    instance_id=runtime.instance_id,
                    generation=record["generation"],
                    status="disconnected",
                    reason="shutdown failed",
                )
                raise
            with self._lock:
                if self._runtimes.get(session_id) is runtime:
                    self._runtimes.pop(session_id)
            self.model.set_transport(
                session_id,
                instance_id=runtime.instance_id if runtime else None,
                generation=record["generation"],
                status="disconnected",
                reason="shut down",
            )

    def close(self, timeout: float = 5.0) -> None:
        with self._lock:
            self._closed = True
            runtimes = tuple(self._runtimes.values())
        close_rpc_manager(
            self,
            runtimes,
            self._refreshes,
            timeout,
            self._operation_lock,
        )

    def _connect(self, record: Mapping[str, Any]) -> None:
        runtime = _Runtime(
            session_id=record["id"],
            instance_id=uuid.uuid4().hex,
            generation=record["generation"],
        )
        with self._lock:
            if self._closed:
                raise RuntimeError("RPC session manager is closed")
            if runtime.session_id in self._runtimes:
                raise RuntimeError(f"session {runtime.session_id} is already connected")
            self._runtimes[runtime.session_id] = runtime
        adapter = None
        try:
            project_path = self._validated_project(record)
            resume_file = recovery_session_file(self.settings.session_root, record)
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
                extra_args = ("--resume", resume_file) if resume_file else ()
                options = {
                    "cwd": project_path,
                    "max_event_history": 512,
                    "max_stderr_chunks": 128,
                }
                client = RpcClient(
                    executable=self.settings.omp_bin,
                    session_dir=self.settings.session_root,
                    extra_args=extra_args,
                    **options,
                )
                command = getattr(client, "command", None)
                if os.name != "nt" or not isinstance(command, tuple):
                    return client
                return RpcClient(
                    command=(sys.executable, "-m", "omp_deck._windows_job", *command),
                    **options,
                )

            adapter = SessionAdapter(
                client_factory,
                on_event=lambda event: self._on_event(runtime, event),
                event_limit=512,
                ui_limit=128,
            )
            runtime.adapter = adapter
            adapter.start()
            # OMP exposes an ID/path before its lazy fresh transcript exists.
            # Rotate before launch is published: new_session durably writes the empty header.
            if resume_file is None:
                materialized = adapter.new_session()
                if getattr(materialized, "cancelled", False):
                    raise RuntimeError("OMP cancelled fresh RPC session materialization")
            with self._lock:
                if self._closed or self._runtimes.get(runtime.session_id) is not runtime:
                    raise RuntimeError("RPC session manager is closed")
            snapshot = adapter.snapshot()
            if resume_file is not None:
                validate_recovered_identity(self.settings.session_root, updated, snapshot.state, resume_file)
            final = self._persist_rpc_identity(updated, snapshot)
            runtime.generation = final["generation"]
            self.model.load_record(final)
            self.model.apply_rpc_snapshot(
                runtime.session_id,
                instance_id=runtime.instance_id,
                generation=runtime.generation,
                snapshot=snapshot,
            )
        except Exception as error:
            cleanup_error = None
            if adapter is not None:
                try:
                    adapter.close()
                except Exception as close_error:
                    cleanup_error = close_error
            if cleanup_error is not None:
                raise RuntimeError(f"{error}; RPC cleanup failed: {cleanup_error}") from error
            with self._lock:
                if self._runtimes.get(runtime.session_id) is runtime:
                    self._runtimes.pop(runtime.session_id)
            raise

    def _persist_rpc_identity(self, record: Mapping[str, Any], snapshot: AdapterSnapshot) -> dict[str, Any]:
        state = snapshot.state
        session_file = state.get("session_file")
        omp_session_id = state.get("session_id")
        recovery = record.get("recoveryMetadata")
        if record.get("ompSessionId") == omp_session_id and recovery == {"sessionFile": session_file}:
            return dict(record)
        canonical_file = validated_session_file(self.settings.session_root, session_file, omp_session_id)
        next_recovery = {"sessionFile": canonical_file}
        if record.get("ompSessionId") == omp_session_id and recovery == next_recovery:
            return dict(record)
        return self.registry.update(
            record["id"],
            omp_session_id=omp_session_id,
            recovery_metadata=next_recovery,
        )

    def _validated_project(self, record: Mapping[str, Any]) -> str:
        project = record["project"]
        resolved = resolve_project_path(self.settings.project_root, project["name"], must_exist=True)
        if str(resolved) != project["path"] or not resolved.is_dir():
            raise ValueError("registered project path is no longer valid")
        return str(resolved)

    def _record(self, session_id: str) -> dict[str, Any]:
        for record in self.registry.load():
            if record["id"] == session_id:
                return record
        raise KeyError(session_id)

    def _mark_refresh_failed(self, session_id: str, reason: str) -> None:
        runtime = self._runtime_or_none(session_id)
        if runtime is None:
            try:
                record = self._record(session_id)
            except KeyError:
                return
            instance_id = None
            generation = record["generation"]
            status = "disconnected"
        else:
            instance_id = runtime.instance_id
            generation = runtime.generation
            status = "degraded"
        self.model.set_transport(
            session_id,
            instance_id=instance_id,
            generation=generation,
            status=status,
            reason=reason,
        )

    def _replace_disconnected(self, session_id: str, error: Exception) -> None:
        with self._operation_lock(session_id):
            runtime = self._runtime_or_none(session_id)
            if runtime is None:
                return
            if runtime.adapter is not None:
                try:
                    runtime.adapter.close()
                except Exception as close_error:
                    self.model.set_transport(
                        session_id,
                        instance_id=runtime.instance_id,
                        generation=runtime.generation,
                        status="disconnected",
                        reason=f"{error}; close failed: {close_error}",
                    )
                    return
            with self._lock:
                if self._runtimes.get(session_id) is runtime:
                    self._runtimes.pop(session_id)
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
            with self._lock:
                if self._closed:
                    return
            try:
                self._connect(record)
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

    def _operation_lock(self, session_id: str) -> threading.RLock:
        with self._lock:
            return self._operation_locks.setdefault(session_id, threading.RLock())

    def _prepare_command(
        self,
        session_id: str,
        action: str,
        payload: Mapping[str, Any],
    ) -> _Runtime:
        runtime = self._connected_runtime(session_id)
        if action == "respondToUi" and isinstance(payload.get("value"), list):
            raise ValueError("RPC UI responses do not support multiple values")
        return runtime

    def _connected_runtime(self, session_id: str) -> _Runtime:
        runtime = self._runtime(session_id)
        if runtime.adapter is None or not self._record(session_id)["desiredRpcRunning"]:
            raise SessionNotConnectedError(session_id)
        return runtime

    def _runtime(self, session_id: str) -> _Runtime:
        runtime = self._runtime_or_none(session_id)
        if runtime is None:
            raise SessionNotConnectedError(session_id)
        return runtime

    def _runtime_or_none(self, session_id: str) -> _Runtime | None:
        with self._lock:
            return self._runtimes.get(session_id)
