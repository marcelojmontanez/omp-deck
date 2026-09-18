"""Local extension connections and validated wire types."""

from __future__ import annotations

import hashlib
import os
import socket
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from ._extension_endpoint import unix_endpoint_name
from ._thinking import normalized_thinking, validated_thinking_levels
from .attachments import LEGACY_EXTENSION_MAX_IMAGE_BYTES, MAX_IMAGE_BYTES, MAX_IMAGES


def _default_connector(path: str):
    if os.name == "nt":
        from ._windows_pipe import connect_named_pipe

        return connect_named_pipe(path)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(path)
    except Exception:
        sock.close()
        raise
    return sock


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
    thinking_levels: tuple[str, ...] = ()
    attachment_limits: Mapping[str, int] | None = None
    attachment_limits_advertised: bool = False


@dataclass(frozen=True)
class ExtensionSnapshot:
    idle: bool
    pending_messages: bool
    model: Mapping[str, str] | None
    async_jobs: Any
    sequence: int
    transport_status: str
    reason: str | None
    thinking: str | None = None
    thinking_levels: tuple[str, ...] = ()


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


def parse_thinking_state(data: Mapping[str, Any]) -> tuple[str | None, tuple[str, ...]]:
    raw_thinking = data.get("thinking")
    thinking = normalized_thinking(raw_thinking)
    if raw_thinking is not None and thinking is None:
        raise ExtensionProtocolError("invalid thinking level")
    try:
        levels = validated_thinking_levels(data.get("thinkingLevels"))
    except ValueError as error:
        raise ExtensionProtocolError(str(error)) from None
    if levels and thinking is not None and thinking not in levels:
        raise ExtensionProtocolError("thinking level is not supported by current model")
    return thinking, levels


def parse_response(frame: dict[str, Any], protocol: str) -> Mapping[str, Any]:
    _exact_keys(frame, {"protocol", "type", "id", "ok"}, {"result", "error"}, "response")
    request_id = frame["id"]
    if (
        frame["protocol"] != protocol
        or frame["type"] != "response"
        or not isinstance(request_id, str)
        or not request_id
        or type(frame["ok"]) is not bool
    ):
        raise ExtensionProtocolError("invalid response envelope")
    if (
        frame["ok"]
        and "error" in frame
        or not frame["ok"]
        and ("result" in frame or not isinstance(frame.get("error"), str))
    ):
        raise ExtensionProtocolError("invalid response")
    return MappingProxyType({key: _immutable(value) for key, value in frame.items()})


def parse_registration(frame: dict[str, Any], socket_path: str, protocol: str) -> ExtensionRegistration:
    required = {
        "protocol",
        "version",
        "type",
        "ompVersion",
        "compatible",
        "connectionId",
        "session",
        "sessionId",
        "title",
        "project",
        "capabilities",
    }
    _exact_keys(frame, required, {"thinkingLevels", "attachmentLimits"}, "registration")
    if (
        frame["protocol"] != protocol
        or frame["version"] != 1
        or frame["type"] != "registration"
        or frame["compatible"] is not True
    ):
        raise ExtensionProtocolError("incompatible registration")
    strings = (frame["ompVersion"], frame["connectionId"], frame["sessionId"], frame["project"])
    if any(not isinstance(value, str) or not value or len(value) > 4096 for value in strings):
        raise ExtensionProtocolError("invalid registration")
    if "\0" in frame["connectionId"] or "\0" in frame["sessionId"]:
        raise ExtensionProtocolError("invalid registration")
    if frame["title"] is not None and (not isinstance(frame["title"], str) or len(frame["title"]) > 4096):
        raise ExtensionProtocolError("invalid registration")
    session = _object(frame["session"], "session")
    _exact_keys(session, {"id", "file", "title"}, set(), "session")
    if (
        session["id"] != frame["sessionId"]
        or session["title"] != frame["title"]
        or (session["file"] is not None and not isinstance(session["file"], str))
    ):
        raise ExtensionProtocolError("invalid session identity")
    caps = _object(frame["capabilities"], "capabilities")
    expected_caps = {
        "sendMessage",
        "followUp",
        "interrupt",
        "shutdown",
        "todos",
        "subagents",
        "answerUiRequest",
        "answer_ui_request",
    }
    _exact_keys(caps, expected_caps, {"attachments", "setThinking"}, "capabilities")
    if any(type(value) is not bool for value in caps.values()):
        raise ExtensionProtocolError("invalid capabilities")
    try:
        thinking_levels = validated_thinking_levels(frame.get("thinkingLevels", ()))
    except ValueError as error:
        raise ExtensionProtocolError(str(error)) from None
    if caps.get("setThinking") is not True and thinking_levels:
        raise ExtensionProtocolError("thinking levels require advertised capability")
    raw_attachment_limits = frame.get("attachmentLimits")
    attachment_limits = None
    if raw_attachment_limits is not None:
        limits = _object(raw_attachment_limits, "attachment limits")
        _exact_keys(limits, {"maxImages", "maxTotalBytes"}, set(), "attachment limits")
        max_images = limits["maxImages"]
        max_total_bytes = limits["maxTotalBytes"]
        if (
            caps.get("attachments") is not True
            or isinstance(max_images, bool)
            or not isinstance(max_images, int)
            or not 1 <= max_images <= MAX_IMAGES
            or isinstance(max_total_bytes, bool)
            or not isinstance(max_total_bytes, int)
            or not 1 <= max_total_bytes <= MAX_IMAGE_BYTES
        ):
            raise ExtensionProtocolError("invalid attachment limits")
        attachment_limits = MappingProxyType({"maxImages": max_images, "maxTotalBytes": max_total_bytes})
    elif caps.get("attachments") is True:
        attachment_limits = MappingProxyType(
            {"maxImages": MAX_IMAGES, "maxTotalBytes": LEGACY_EXTENSION_MAX_IMAGE_BYTES}
        )
    basename = os.path.basename(socket_path)
    if os.name == "nt":
        socket_id = hashlib.sha256(frame["sessionId"].encode("utf-8")).hexdigest()[:24]
        valid_identity = basename.startswith(socket_id + ".") and basename.endswith(
            "." + frame["connectionId"] + ".pipe"
        )
    else:
        valid_identity = basename == unix_endpoint_name(frame["sessionId"], frame["connectionId"])
    if not valid_identity:
        raise ExtensionProtocolError("registration socket identity mismatch")
    return ExtensionRegistration(
        protocol_version=1,
        omp_version=frame["ompVersion"],
        session_id=frame["sessionId"],
        project=frame["project"],
        title=frame["title"],
        connection_id=frame["connectionId"],
        capabilities=MappingProxyType(dict(caps)),
        thinking_levels=thinking_levels,
        attachment_limits=attachment_limits,
        attachment_limits_advertised=raw_attachment_limits is not None,
    )


def snapshot_from_data(
    data: Any,
    sequence: int,
    registration: ExtensionRegistration | None,
    thinking_levels: tuple[str, ...],
) -> ExtensionSnapshot:
    data = _object(data, "snapshot")
    _exact_keys(
        data,
        {"idle", "pendingMessages", "model", "async"},
        {"eventSequence", "thinking", "thinkingLevels"},
        "snapshot",
    )
    if type(data["idle"]) is not bool or type(data["pendingMessages"]) is not bool:
        raise ExtensionProtocolError("invalid snapshot")
    model = data["model"]
    frozen_model = None
    if model is not None:
        model = _object(model, "model")
        _exact_keys(model, {"provider", "id"}, set(), "model")
        if any(not isinstance(value, str) or not value or len(value) > 1024 for value in model.values()):
            raise ExtensionProtocolError("invalid model")
        frozen_model = MappingProxyType(dict(model))
    thinking = None
    levels = thinking_levels
    if "thinking" in data or "thinkingLevels" in data:
        if "thinking" not in data or "thinkingLevels" not in data:
            raise ExtensionProtocolError("incomplete thinking state")
        if registration is None or registration.capabilities.get("setThinking") is not True:
            raise ExtensionProtocolError("unadvertised thinking state")
        thinking, levels = parse_thinking_state(data)
    return ExtensionSnapshot(
        idle=data["idle"],
        pending_messages=data["pendingMessages"],
        model=frozen_model,
        async_jobs=_immutable(data["async"]),
        sequence=sequence,
        transport_status="connected",
        reason=None,
        thinking=thinking,
        thinking_levels=levels,
    )
