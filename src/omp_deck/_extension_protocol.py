"""Validated wire types for the local OMP extension protocol."""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any


class ExtensionAdapterError(RuntimeError):
    """Base adapter error."""


class ExtensionProtocolError(ExtensionAdapterError):
    """The extension sent an invalid protocol frame."""


class ExtensionDisconnected(ExtensionAdapterError):
    """The extension connection closed before an operation completed."""


class ExtensionRequestTimeout(ExtensionAdapterError):
    """A request received no response before its deadline."""


@dataclass(frozen=True)
class ExtensionRegistration:
    protocol_version: int
    omp_version: str
    session_id: str
    project: str
    title: str | None
    connection_id: str
    capabilities: Mapping[str, bool]


@dataclass(frozen=True)
class ExtensionSnapshot:
    idle: bool
    pending_messages: bool
    model: Mapping[str, str] | None
    async_jobs: Any
    sequence: int
    transport_status: str
    reason: str | None


@dataclass
class _Pending:
    ready: threading.Event
    response: Mapping[str, Any] | None = None
    disconnected: bool = False


def _immutable(value: Any, *, depth: int = 0, nodes: list[int] | None = None) -> Any:
    if nodes is None:
        nodes = [0]
    nodes[0] += 1
    if depth > 24 or nodes[0] > 20_000:
        raise ExtensionProtocolError("payload bounds exceeded")
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return tuple(_immutable(item, depth=depth + 1, nodes=nodes) for item in value)
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ExtensionProtocolError("invalid payload")
        return MappingProxyType({key: _immutable(item, depth=depth + 1, nodes=nodes) for key, item in value.items()})
    raise ExtensionProtocolError("invalid payload")


def _object(frame: Any, label: str) -> dict[str, Any]:
    if not isinstance(frame, Mapping):
        raise ExtensionProtocolError(f"invalid {label}")
    return dict(frame)


def _exact_keys(value: Mapping[str, Any], required: set[str], optional: set[str], label: str) -> None:
    keys = set(value)
    if not required <= keys or keys - required - optional:
        raise ExtensionProtocolError(f"invalid {label}")
