from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from . import _rpc_commands
from .request_ledger import RequestConflictError


class SessionNotConnectedError(LookupError):
    pass


class SessionCommandConflictError(ValueError):
    pass


def _refresh_if_desired(manager: Any, session_id: str) -> None:
    try:
        desired = manager._record(session_id)["desiredRpcRunning"]
    except KeyError:
        return
    if desired:
        manager._refreshes.request((session_id,))


def dispatch(
    manager: Any,
    session_id: str,
    request_id: str,
    action: str,
    payload: Mapping[str, Any],
    conflict_error: type[ValueError],
    disconnected_error: type[LookupError],
) -> dict[str, Any]:
    if action == "shutdown":
        try:
            return manager._requests.dispatch(
                session_id,
                request_id,
                action,
                payload,
                prepare=lambda: manager._prepare_command(session_id, action, payload),
                execute=lambda runtime, command, body: execute(manager, runtime, command, body, disconnected_error),
                shutdown=True,
            )
        except RequestConflictError:
            raise conflict_error("requestId was already used for another command") from None
    priority = action in {"respondToUi", "cancelUi"}
    operation = manager._operation_lock(session_id)
    if priority:
        try:
            return manager._requests.dispatch(
                session_id,
                request_id,
                action,
                payload,
                prepare=lambda: manager._prepare_command(session_id, action, payload),
                execute=lambda runtime, command, body: execute(manager, runtime, command, body, disconnected_error),
                priority=True,
            )
        except RequestConflictError:
            raise conflict_error("requestId was already used for another command") from None
    with operation:
        try:
            return manager._requests.dispatch(
                session_id,
                request_id,
                action,
                payload,
                prepare=lambda: manager._prepare_command(session_id, action, payload),
                execute=lambda runtime, command, body: execute(manager, runtime, command, body, disconnected_error),
            )
        except RequestConflictError:
            raise conflict_error("requestId was already used for another command") from None


def command(
    manager: Any,
    session_id: str,
    action: str,
    payload: Mapping[str, Any],
    disconnected_error: type[LookupError],
) -> dict[str, Any]:
    if action in {"respondToUi", "cancelUi"}:
        return execute(manager, manager._connected_runtime(session_id), action, payload, disconnected_error)
    if action == "shutdown":
        manager.shutdown(session_id)
        return {"accepted": True, "result": None}
    with manager._operation_lock(session_id):
        return execute(manager, manager._connected_runtime(session_id), action, payload, disconnected_error)


def execute(
    manager: Any,
    runtime: Any,
    action: str,
    payload: Mapping[str, Any],
    disconnected_error: type[LookupError],
) -> dict[str, Any]:
    session_id = runtime.session_id
    if action == "shutdown":
        manager.shutdown(session_id)
        return {"accepted": True, "result": None}
    adapter = runtime.adapter
    if adapter is None:
        raise disconnected_error(session_id)
    result = _rpc_commands.invoke(adapter, action, payload)
    if action in {"respondToUi", "cancelUi"}:
        _refresh_if_desired(manager, session_id)
        return {"accepted": True, "result": _rpc_commands.plain(result)}
    try:
        snapshot = adapter.snapshot()
        with manager._lock:
            shutting_down = runtime.session_id in manager._shutdown_requested
            current = manager._runtimes.get(session_id) is runtime
            if not current and not shutting_down:
                return {"accepted": True, "result": _rpc_commands.plain(result)}
            if action in {"branch", "switchSession"}:
                final = manager._persist_rpc_identity(manager._record(session_id), snapshot)
                runtime.generation = final["generation"]
                manager.model.load_record(final)
            if shutting_down:
                return {"accepted": True, "result": _rpc_commands.plain(result)}
            manager.model.apply_rpc_snapshot(
                session_id,
                instance_id=runtime.instance_id,
                generation=runtime.generation,
                snapshot=snapshot,
            )
    except Exception:
        _refresh_if_desired(manager, session_id)
    return {"accepted": True, "result": _rpc_commands.plain(result)}
