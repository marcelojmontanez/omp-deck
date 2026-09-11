"""Immutable Collab transport data and stateless connection helpers."""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from cryptography.exceptions import InvalidTag

from .rpc_adapter import AdapterDisconnectedError, AdapterError, AdapterIncompatibleError

_CONNECT_OPTIONS = {
    "open_timeout": 10.0,
    "close_timeout": 10.0,
    "ping_interval": 20.0,
    "ping_timeout": 20.0,
    "max_queue": 16,
}
_FATAL_CLOSE_CODES = frozenset({1002, 1003, 1007, 1008, 1009, 1010, 4001, 4004, 4009, 4029})


class _Socket(Protocol):
    def send(self, data: bytes) -> Any: ...
    def recv(self, timeout: float | None = None) -> str | bytes: ...
    def close(self) -> Any: ...


def _close_socket(socket: _Socket | None) -> None:
    if socket is None:
        return
    try:
        socket.close()
    except Exception:
        pass


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class CollabSnapshot:
    header: Mapping[str, Any]
    state: Mapping[str, Any]
    entries: tuple[Mapping[str, Any], ...]
    agents: tuple[Mapping[str, Any], ...]
    pending_ui_requests: tuple[Mapping[str, Any], ...]
    sequence: int
    transport_status: str
    reason: str | None
    read_only: bool


@dataclass(slots=True)
class _Reply:
    event: threading.Event
    value: tuple[str, int] | BaseException | None = None


def _guest_url(websocket_url: str) -> str:
    parsed = urlsplit(websocket_url)
    query = dict(parse_qsl(parsed.query))
    query["role"] = "guest"
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))


def _safe_reason(error: BaseException) -> str:
    if isinstance(error, (InvalidTag, ValueError)):
        return "invalid encrypted collaboration frame"
    return "collaboration transport disconnected"


def _classify_fatal(error: BaseException) -> AdapterError | None:
    if isinstance(error, AdapterIncompatibleError):
        return error
    if isinstance(error, AdapterDisconnectedError) and error.kind == "relay":
        return error
    if isinstance(error, (InvalidTag, ValueError)):
        return AdapterIncompatibleError("invalid encrypted collaboration frame", kind="crypto")
    code = getattr(error, "code", None) or getattr(getattr(error, "rcvd", None), "code", None)
    if code in _FATAL_CLOSE_CODES:
        return AdapterDisconnectedError("relay rejected collaboration connection", kind="relay")
    return None


def _normalize_event_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Translate official Collab event names to the normalized adapter contract."""
    names = {
        "toolCallId": "tool_call_id",
        "toolName": "tool_name",
        "partialResult": "partial_result",
        "isError": "is_error",
        "isTerminal": "is_terminal",
        "assistantMessageEvent": "assistant_message_event",
        "thinkingLevel": "thinking_level",
        "maxAttempts": "max_attempts",
        "delayMs": "delay_ms",
        "errorMessage": "error_message",
        "errorId": "error_id",
        "finalError": "final_error",
        "retryErrors": "retry_errors",
        "willRetry": "will_retry",
    }
    return {names.get(key, key): value for key, value in payload.items()}


def _validate_relay_control(raw: str, max_frame_bytes: int) -> None:
    if len(raw.encode("utf-8")) > max_frame_bytes:
        raise AdapterIncompatibleError("relay control frame exceeds maximum size", kind="bounds")
    try:
        control = json.loads(raw, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AdapterIncompatibleError("relay sent an invalid control frame", kind="protocol") from exc
    if not isinstance(control, dict) or set(control) - {"type", "peerId", "reason"}:
        raise AdapterIncompatibleError("relay sent an unexpected control frame", kind="protocol")
    kind, peer_id = control.get("type"), control.get("peerId")
    if (
        kind in ("peer-joined", "peer-left")
        and isinstance(peer_id, int)
        and not isinstance(peer_id, bool)
        and 0 <= peer_id <= 0xFFFFFFFF
        and set(control) == {"type", "peerId"}
    ):
        return
    if (
        kind == "room-closed"
        and set(control) <= {"type", "reason"}
        and (control.get("reason") is None or isinstance(control.get("reason"), str))
    ):
        raise AdapterDisconnectedError("collaboration room was closed", kind="relay")
    raise AdapterIncompatibleError("relay sent an unexpected control frame", kind="protocol")


def _fail_replies(replies: Mapping[int, _Reply], error: BaseException) -> None:
    for reply in replies.values():
        if not reply.event.is_set():
            reply.value = error
            reply.event.set()
