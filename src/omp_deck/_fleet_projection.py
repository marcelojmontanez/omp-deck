from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from ._attachment_limits import extension_attachment_limits, platform_attachment_limits
from ._extension_jobs import normalize_async_jobs
from ._platform_capabilities import platform_capabilities
from ._session_activity import completion_state
from ._thinking import normalized_thinking, thinking_levels_for_model
from .attachments import assistant_image_content, decode_image
from .rpc_adapter import AdapterSnapshot, PendingUiRequest

MAX_MESSAGES = 500
MAX_MESSAGE_BYTES = 2 * 1024 * 1024
MAX_COLLECTION_ITEMS = 500


def now() -> str:
    return datetime.now(UTC).isoformat()


def plain_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): plain_json(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [plain_json(item) for item in value]
    return value


def strict_clone(value: Any) -> Any:
    return json.loads(json.dumps(plain_json(value), ensure_ascii=False, allow_nan=False, separators=(",", ":")))


def _timestamp(value: object) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value / 1000, UTC).isoformat()
    if isinstance(value, str) and value:
        return value
    return datetime.fromtimestamp(0, UTC).isoformat()


def _model_identity(state: Mapping[str, Any]) -> dict[str, Any] | None:
    value = state.get("model")
    if not isinstance(value, Mapping):
        return None
    result = {key: value.get(key) for key in ("provider", "id", "name", "api") if isinstance(value.get(key), str)}
    return result or None


def _message_content(message: Mapping[str, Any]) -> list[Any]:
    content = message.get("content")
    if isinstance(content, str):
        values: Sequence[Any] = [{"type": "text", "text": content}]
    elif isinstance(content, Sequence) and not isinstance(content, (str, bytes, bytearray)):
        values = content
    else:
        values = []
    result = []
    for value in values:
        if not isinstance(value, Mapping):
            continue
        if value.get("type") == "text" and isinstance(value.get("text"), str):
            if message.get("role") == "assistant":
                result.extend(assistant_image_content(value["text"]))
            else:
                result.append({"type": "text", "text": value["text"]})
        elif value.get("type") == "image":
            data = value.get("data")
            digest = data.removeprefix("blob:sha256:") if isinstance(data, str) else ""
            if isinstance(data, str) and not data.startswith("blob:sha256:"):
                try:
                    digest = hashlib.sha256(decode_image(data, value.get("mimeType"))).hexdigest()
                except ValueError:
                    digest = ""
            url = value.get("url")
            if (
                not digest
                and isinstance(url, str)
                and url.startswith("/api/blob/")
                and len(url) == len("/api/blob/") + 64
            ):
                digest = url.removeprefix("/api/blob/")
            if len(digest) == 64 and all(character in "0123456789abcdef" for character in digest):
                result.append({"type": "image", "url": f"/api/blob/{digest}"})
            else:
                result.append({"type": "image"})
    error = message.get("errorMessage")
    if isinstance(error, str) and error.strip():
        result.append({"type": "text", "text": error})
    return result


def has_visible_content(message: Mapping[str, Any]) -> bool:
    content = message.get("content")
    if not isinstance(content, Sequence) or isinstance(content, (str, bytes, bytearray)):
        return False
    return any(
        isinstance(item, Mapping)
        and (
            item.get("type") == "image"
            or (item.get("type") == "text" and isinstance(item.get("text"), str) and bool(item["text"].strip()))
        )
        for item in content
    )


def normalize_messages(
    values: Sequence[Mapping[str, Any]], session_id: str, *, offset: int = 0
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    start = max(0, len(values) - MAX_MESSAGES)
    for index, raw in enumerate(values[start:], start=start + offset):
        message_id = raw.get("id")
        if not isinstance(message_id, str) or not message_id:
            canonical = json.dumps(
                plain_json(raw), sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            )
            message_id = hashlib.sha256(f"{session_id}\0{index}\0{canonical}".encode()).hexdigest()
        normalized.append(
            {
                "id": message_id,
                "role": str(raw.get("role", "unknown")),
                "content": _message_content(raw),
                "createdAt": _timestamp(raw.get("timestamp")),
            }
        )
    return bounded_messages(normalized)


def bounded_messages(messages: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    retained_bytes = 0
    start = len(messages)
    for index in range(len(messages) - 1, max(-1, len(messages) - MAX_MESSAGES - 1), -1):
        size = len(json.dumps(messages[index], ensure_ascii=False, allow_nan=False).encode("utf-8"))
        if retained_bytes + size > MAX_MESSAGE_BYTES:
            break
        retained_bytes += size
        start = index
    return list(messages[start:])


def message_identity(message: Mapping[str, Any]) -> tuple[Any, ...] | None:
    if isinstance(message.get("id"), str) and message["id"]:
        return ("id", message["id"])
    timestamp = message.get("timestamp")
    return (message.get("role"), timestamp) if isinstance(timestamp, (str, int, float)) else None


def bounded_collection(values: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [strict_clone(value) for value in values[-MAX_COLLECTION_ITEMS:]]


def normalize_todos(values: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    normalized = []
    for value in values:
        tasks = value.get("tasks")
        if isinstance(tasks, Sequence) and not isinstance(tasks, (str, bytes)):
            for task in tasks:
                if isinstance(task, Mapping):
                    row = strict_clone(task)
                    row["phase"] = str(value.get("name") or "Tasks")
                    normalized.append(row)
                    if len(normalized) == MAX_COLLECTION_ITEMS:
                        return normalized
        else:
            normalized.append(strict_clone(value))
            if len(normalized) == MAX_COLLECTION_ITEMS:
                return normalized
    return normalized


def todos_from_tool_result(tool_name: object, result: object) -> list[dict[str, Any]] | None:
    if tool_name != "todo" or not isinstance(result, Mapping):
        return None
    details = result.get("details")
    phases = details.get("phases") if isinstance(details, Mapping) else None
    if not isinstance(phases, list) or any(not isinstance(phase, Mapping) for phase in phases):
        return None
    return normalize_todos(phases)


def pending_ui_projection(
    value: PendingUiRequest | Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if value is None:
        return None
    request = asdict(value) if isinstance(value, PendingUiRequest) else value
    request_id = request.get("id")
    kind = request.get("kind")
    if not isinstance(request_id, str) or not request_id or not isinstance(kind, str):
        return None
    options = []
    for option in request.get("options", ()):
        if not isinstance(option, Mapping):
            continue
        option_value = option.get("value")
        label = option.get("label")
        if not isinstance(option_value, str) or not isinstance(label, str):
            continue
        description = option.get("description")
        options.append(
            {
                "value": option_value,
                "label": label,
                "description": description if isinstance(description, str) else None,
            }
        )
    title = request.get("title")
    message = request.get("message")
    prompt = message or title or kind
    expires_at = request.get("expires_at")
    question = {
        "id": f"{request_id}:0",
        "prompt": prompt,
        "options": options,
        "multi": False,
        "recommended": None,
        "allowCustom": request.get("allow_custom") is True,
        "customPlaceholder": request.get("custom_placeholder")
        if isinstance(request.get("custom_placeholder"), str)
        else None,
        "prefill": request.get("prefill") if isinstance(request.get("prefill"), str) else None,
    }
    return {
        "id": request_id,
        "kind": kind,
        "title": title if isinstance(title, str) else None,
        "message": message if isinstance(message, str) else None,
        "questions": [question],
        "questionIndex": 0,
        "allowCancel": request.get("allow_cancel") is True,
        "expiresAt": _timestamp(expires_at * 1000)
        if isinstance(expires_at, (int, float)) and not isinstance(expires_at, bool)
        else None,
        "status": "pending",
    }


def _collab_pending(request: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if request is None:
        return None
    request_id, kind = request.get("reqId"), request.get("kind")
    if not isinstance(request_id, int) or isinstance(request_id, bool) or kind not in {"select", "editor"}:
        return None
    options = []
    for option in request.get("options", []):
        if isinstance(option, str):
            options.append({"label": option, "description": None})
        elif isinstance(option, Mapping):
            options.append(
                {
                    "label": str(option.get("label", "")),
                    "description": option.get("description") if isinstance(option.get("description"), str) else None,
                }
            )
    toggle_selection = request.get("selectionMarker") == "checkbox"
    initial = request.get("initialIndex")
    question = {
        "id": f"{request_id}:0",
        "prompt": request.get("title") or kind,
        "options": options,
        "multi": False,
        "toggleSelection": toggle_selection,
        "recommended": initial if isinstance(initial, int) and not isinstance(initial, bool) else None,
        "checkedIndices": list(request.get("checkedIndices", [])) if toggle_selection else [],
        "allowCustom": kind == "editor",
        "customPlaceholder": None,
        "prefill": request.get("prefill") if isinstance(request.get("prefill"), str) else None,
    }
    return {
        "id": str(request_id),
        "kind": kind,
        "title": request.get("title"),
        "message": request.get("helpText"),
        "questions": [question],
        "questionIndex": 0,
        "allowCancel": True,
        "expiresAt": None,
        "status": "pending",
    }


def rpc_projection(
    session: Mapping[str, Any], snapshot: AdapterSnapshot, session_id: str, instance_id: str, generation: int
) -> dict[str, Any]:
    pending = pending_ui_projection(snapshot.pending_ui_request)
    state = snapshot.state
    thinking_levels = thinking_levels_for_model(state.get("model"))
    capabilities = platform_capabilities("rpc")
    capabilities["setThinking"] = bool(thinking_levels)
    public_state = (
        "disconnected"
        if snapshot.transport_status != "connected"
        else (
            "needs_input"
            if pending is not None
            else "working"
            if state.get("is_streaming") or state.get("is_compacting")
            else completion_state(snapshot.messages)
        )
    )
    messages = normalize_messages(snapshot.messages, session_id)
    stream_key = session.get("_rpcStreamingKey")
    retain_stream = (
        snapshot.transport_status == "connected"
        and state.get("is_streaming")
        and session.get("_generation") == generation
        and session["transport"]["instanceId"] == instance_id
        and not session.get("_resyncRequired")
        and not (stream_key is not None and any(message_identity(item) == stream_key for item in snapshot.messages))
    )
    return {
        "ompSessionId": state.get("session_id") or session.get("ompSessionId"),
        "title": state.get("session_name") or session["title"],
        "state": public_state,
        "transport": {
            "kind": "rpc",
            "status": snapshot.transport_status,
            "label": "Managed RPC",
            "instanceId": instance_id,
            "reason": snapshot.reason,
        },
        "capabilities": capabilities,
        "attachmentLimits": platform_attachment_limits("rpc"),
        "messages": messages,
        "streamingMessage": session.get("streamingMessage") if retain_stream else None,
        "todos": normalize_todos(snapshot.todos),
        "subagents": bounded_collection(snapshot.subagents),
        "subagentError": None,
        "pendingUiRequest": pending,
        "workingOn": None if not state.get("is_streaming") else session.get("workingOn"),
        "model": _model_identity(state),
        "thinking": normalized_thinking(state.get("thinking_level")),
        "thinkingLevels": list(thinking_levels),
        "_rpcMessageCount": len(snapshot.messages),
        "_rpcLastMessageKey": message_identity(snapshot.messages[-1]) if snapshot.messages else None,
        "_rpcLastMessageId": messages[-1]["id"] if messages else None,
        "_rpcStreamingIndex": session.get("_rpcStreamingIndex") if retain_stream else None,
        "_rpcStreamingKey": stream_key if retain_stream else None,
        "_generation": generation,
        "_adapterSequence": snapshot.sequence,
        "_resyncRequired": False,
        "_resyncWatermark": 0,
    }


def collab_projection(
    session: Mapping[str, Any], snapshot: Any, session_id: str, instance_id: str, generation: int
) -> dict[str, Any]:
    pending = _collab_pending(snapshot.pending_ui_requests[-1] if snapshot.pending_ui_requests else None)
    state = snapshot.state
    capabilities = platform_capabilities("collab")
    if snapshot.read_only:
        for capability in ("sendMessage", "interrupt", "answerUiRequest", "cancelUiRequest", "attachments"):
            capabilities[capability] = False
    entries = []
    for entry in snapshot.entries:
        if entry.get("type") == "message" and isinstance(entry.get("message"), Mapping):
            message = dict(entry["message"])
        elif (
            entry.get("type") == "custom_message"
            and entry.get("customType") == "collab-prompt"
            and entry.get("display") is True
            and isinstance(entry.get("content"), str)
        ):
            message = {"role": "user", "content": entry["content"]}
        else:
            continue
        message["id"] = entry.get("id")
        message["timestamp"] = entry.get("timestamp")
        entries.append(message)
    public_state = (
        "disconnected"
        if snapshot.transport_status != "connected"
        else (
            "needs_input"
            if pending is not None
            else "working"
            if state.get("isStreaming")
            else completion_state(entries)
        )
    )
    return {
        "ompSessionId": snapshot.header.get("id") or session.get("ompSessionId"),
        "title": state.get("sessionName") or session["title"],
        "state": public_state,
        "transport": {
            "kind": "collab",
            "status": snapshot.transport_status,
            "label": "Terminal session via Collab",
            "instanceId": instance_id,
            "reason": snapshot.reason,
        },
        "capabilities": capabilities,
        "attachmentLimits": (
            platform_attachment_limits("collab")
            if capabilities["attachments"] and snapshot.transport_status == "connected"
            else None
        ),
        "messages": normalize_messages(entries, session_id),
        "subagents": bounded_collection([agent for agent in snapshot.agents if agent.get("kind") == "sub"]),
        "pendingUiRequest": pending,
        "workingOn": session.get("workingOn") if state.get("isStreaming") else None,
        "model": _model_identity(state),
        "thinking": normalized_thinking(state.get("thinkingLevel")),
        "thinkingLevels": [],
        "_generation": generation,
        "_adapterSequence": snapshot.sequence,
        "_resyncRequired": False,
        "_resyncWatermark": 0,
    }


def extension_projection(
    session: Mapping[str, Any], registration: Any, snapshot: Any, instance_id: str, generation: int
) -> dict[str, Any]:
    thinking_control = registration.capabilities.get("setThinking") is True
    thinking_levels = snapshot.thinking_levels if thinking_control else ()
    capabilities = platform_capabilities("extension")
    for capability in capabilities:
        if capability != "remove" and capability in registration.capabilities:
            capabilities[capability] = capabilities[capability] and bool(registration.capabilities[capability])
    capabilities["setThinking"] = thinking_control and bool(thinking_levels)
    capabilities["attachments"] = (
        registration.capabilities.get("attachments") is True and snapshot.transport_status == "connected"
    )
    subagents = normalize_async_jobs(snapshot.async_jobs)
    pending = None if snapshot.idle else session.get("pendingUiRequest")
    public_state = (
        "disconnected"
        if snapshot.transport_status != "connected"
        else "needs_input"
        if pending is not None
        else "failed"
        if snapshot.idle and session.get("state") == "failed"
        else "idle"
        if snapshot.idle
        else "working"
    )
    return {
        "ompSessionId": registration.session_id,
        "title": registration.title or session["title"],
        "project": {"name": session["project"]["name"], "path": registration.project},
        "state": public_state,
        "transport": {
            "kind": "extension",
            "status": snapshot.transport_status,
            "label": "Terminal session via extension",
            "instanceId": instance_id,
            "reason": snapshot.reason,
        },
        "capabilities": capabilities,
        "attachmentLimits": extension_attachment_limits(registration, capabilities["attachments"]),
        "subagents": bounded_collection(subagents),
        "pendingUiRequest": pending,
        "workingOn": None if snapshot.idle else session.get("workingOn"),
        "model": _model_identity({"model": snapshot.model}),
        "thinking": normalized_thinking(snapshot.thinking) if thinking_control else None,
        "thinkingLevels": list(thinking_levels),
        "_extensionThinkingControl": thinking_control,
        "_generation": generation,
        "_adapterSequence": snapshot.sequence,
        "_resyncRequired": False,
        "_resyncWatermark": 0,
    }
