from __future__ import annotations

from typing import Any

from omp_rpc import (
    RpcCommandError,
    RpcConcurrencyError,
    RpcError,
    RpcProcessExitError,
    RpcProtocolError,
    RpcTimeoutError,
)

from ._rpc_subagents import SubagentProtocolError


class AdapterError(RuntimeError):
    """Base error retaining the transport failure category."""

    def __init__(self, message: str, *, kind: str, cause: BaseException | None = None):
        super().__init__(message)
        self.kind = kind
        self.__cause__ = cause


class AdapterTimeoutError(AdapterError):
    pass


class AdapterCancelledError(AdapterError):
    pass


class AdapterDisconnectedError(AdapterError):
    pass


class AdapterIncompatibleError(AdapterError):
    pass


def project_protocol_error(error: RpcProtocolError) -> tuple[str, dict[str, Any]]:
    payload: dict[str, Any] = {"error": str(error)}
    if not isinstance(error, RpcProtocolError):
        return "protocol_error", payload
    if error.command is not None:
        payload["command"] = error.command
    if error.request_id is not None:
        payload["nativeRequestId"] = error.request_id
    code = error.payload.get("code")
    if isinstance(code, str) and code:
        payload["errorCode"] = code
    if (
        error.command in {"prompt", "abort_and_prompt"}
        and error.request_id is not None
        and error.remote_error is not None
    ):
        payload["error"] = error.remote_error
        payload.setdefault("errorCode", "command")
        payload["correlationAvailable"] = False
        return "native_delivery_failed", payload
    return "protocol_error", payload


def translate_error(exc: BaseException) -> AdapterError:
    if isinstance(exc, AdapterError):
        return exc
    if isinstance(exc, (RpcTimeoutError, TimeoutError)):
        return AdapterTimeoutError(str(exc), kind="timeout", cause=exc)
    if isinstance(exc, RpcProcessExitError):
        return AdapterDisconnectedError(str(exc), kind="process_exit", cause=exc)
    if isinstance(exc, (RpcProtocolError, RpcConcurrencyError, SubagentProtocolError)):
        return AdapterIncompatibleError(str(exc), kind="protocol", cause=exc)
    if isinstance(exc, RpcCommandError):
        text = str(exc)
        cancellation = f"{exc.code or ''} {exc.error}".lower()
        if any(marker in cancellation for marker in ("cancel", "abort")):
            return AdapterCancelledError(text, kind="cancelled", cause=exc)
        return AdapterError(text, kind="command", cause=exc)
    if isinstance(exc, (BrokenPipeError, EOFError, ConnectionError)):
        return AdapterDisconnectedError(str(exc), kind="disconnect", cause=exc)
    if isinstance(exc, RpcError):
        return AdapterError(str(exc), kind="rpc", cause=exc)
    return AdapterError(str(exc), kind="internal", cause=exc)
