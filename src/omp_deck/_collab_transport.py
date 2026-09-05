"""Immutable Collab transport data and stateless connection helpers."""

from __future__ import annotations

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


def _fail_replies(replies: Mapping[int, _Reply], error: BaseException) -> None:
    for reply in replies.values():
        if not reply.event.is_set():
            reply.value = error
            reply.event.set()
