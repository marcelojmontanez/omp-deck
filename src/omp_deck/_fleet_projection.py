from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .rpc_adapter import AdapterSnapshot, PendingUiRequest

CAPABILITIES: dict[str, dict[str, bool]] = {
    "rpc": {"sendMessage": True, "followUp": True, "interrupt": True, "shutdown": True,
            "setModel": True, "setThinking": True, "answerUiRequest": True,
            "cancelUiRequest": True, "subagents": True, "todos": True, "attachments": True,
            "branch": True, "switch": True, "resume": True, "compact": True},
    "collab": {"sendMessage": True, "followUp": False, "interrupt": True, "shutdown": False,
               "setModel": False, "setThinking": False, "answerUiRequest": True,
               "cancelUiRequest": True, "subagents": True, "todos": True, "attachments": True,
               "branch": False, "switch": False, "resume": False, "compact": False},
    "extension": {"sendMessage": True, "followUp": True, "interrupt": True, "shutdown": True,
                  "setModel": False, "setThinking": False, "answerUiRequest": False,
                  "cancelUiRequest": False, "subagents": True, "todos": True, "attachments": False,
                  "branch": False, "switch": False, "resume": False, "compact": False},
}

MAX_MESSAGES = 500
MAX_MESSAGE_BYTES = 2 * 1024 * 1024
MAX_COLLECTION_ITEMS = 500


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
        return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat()
    if isinstance(value, str) and value:
        return value
    return datetime.fromtimestamp(0, timezone.utc).isoformat()


def _model_identity(state: Mapping[str, Any]) -> dict[str, Any] | None:
    value = state.get("model")
    if not isinstance(value, Mapping):
        return None
    result = {key: value.get(key) for key in ("provider", "id", "name", "api") if isinstance(value.get(key), str)}
    return result or None


def _message_content(message: Mapping[str, Any]) -> list[Any]:
    content = message.get("content")
    if isinstance(content, str):
        values: list[Any] = [{"type": "text", "text": content}]
    elif isinstance(content, list):
        values = content
    else:
        text = message.get("output") or message.get("summary") or message.get("command")
        values = [{"type": "text", "text": text}] if isinstance(text, str) else []
    result = []
    for value in values:
        if not isinstance(value, Mapping):
            continue
        if value.get("type") == "text" and isinstance(value.get("text"), str):
            result.append({"type": "text", "text": value["text"]})
        elif value.get("type") == "image":
            data = value.get("data")
            digest = data.removeprefix("blob:sha256:") if isinstance(data, str) else ""
            if len(digest) == 64 and all(character in "0123456789abcdef" for character in digest):
                result.append({"type": "image", "url": f"/api/blob/{digest}"})
            else:
                result.append({"type": "image"})
    return result


def normalize_messages(values: Sequence[Mapping[str, Any]], session_id: str) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    retained_bytes = 0
    start = max(0, len(values) - MAX_MESSAGES)
    for index, raw in enumerate(values[start:], start=start):
        canonical = json.dumps(raw, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        raw_id = raw.get("id")
        message_id = raw_id if isinstance(raw_id, str) and raw_id else hashlib.sha256(
            f"{session_id}\0{index}\0{canonical}".encode("utf-8")
        ).hexdigest()
        message = {"id": message_id, "role": str(raw.get("role", "unknown")),
                   "content": _message_content(raw), "createdAt": _timestamp(raw.get("timestamp"))}
        encoded_size = len(json.dumps(message, ensure_ascii=False, allow_nan=False).encode("utf-8"))
        normalized.append(message)
        retained_bytes += encoded_size
        while normalized and retained_bytes > MAX_MESSAGE_BYTES:
            removed = normalized.pop(0)
            retained_bytes -= len(json.dumps(removed, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    return normalized


def bounded_collection(values: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [strict_clone(value) for value in values[-MAX_COLLECTION_ITEMS:]]


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
            options.append({"label": str(option.get("label", "")),
                            "description": option.get("description") if isinstance(option.get("description"), str) else None})
    toggle_selection = request.get("selectionMarker") == "checkbox"
    initial = request.get("initialIndex")
    question = {"id": f"{request_id}:0", "prompt": request.get("title") or kind, "options": options,
                "multi": False, "toggleSelection": toggle_selection,
                "recommended": initial if isinstance(initial, int) and not isinstance(initial, bool) else None,
                "checkedIndices": list(request.get("checkedIndices", [])) if toggle_selection else [],
                "allowCustom": kind == "editor", "customPlaceholder": None,
                "prefill": request.get("prefill") if isinstance(request.get("prefill"), str) else None}
    return {"id": str(request_id), "kind": kind, "title": request.get("title"),
            "message": request.get("helpText"), "questions": [question], "questionIndex": 0,
            "allowCancel": True, "expiresAt": None, "status": "pending"}


def rpc_projection(session: Mapping[str, Any], snapshot: AdapterSnapshot, session_id: str,
                   instance_id: str, generation: int) -> dict[str, Any]:
    pending = pending_ui_projection(snapshot.pending_ui_request)
    state = snapshot.state
    public_state = "disconnected" if snapshot.transport_status != "connected" else (
        "needs_input" if pending is not None else "working" if state.get("is_streaming") or state.get("is_compacting") else "idle")
    return {"ompSessionId": state.get("session_id") or session.get("ompSessionId"), "state": public_state,
            "transport": {"kind": "rpc", "status": snapshot.transport_status, "label": "Managed RPC",
                          "instanceId": instance_id, "reason": snapshot.reason},
            "messages": normalize_messages(snapshot.messages, session_id), "todos": bounded_collection(snapshot.todos),
            "subagents": bounded_collection(snapshot.subagents), "pendingUiRequest": pending, "updatedAt": now(),
            "workingOn": None if not state.get("is_streaming") else session.get("workingOn"),
            "model": _model_identity(state), "thinking": state.get("thinking_level"), "_generation": generation,
            "_adapterSequence": snapshot.sequence, "_resyncRequired": False, "_resyncWatermark": 0}


def collab_projection(session: Mapping[str, Any], snapshot: Any, session_id: str,
                      instance_id: str, generation: int) -> dict[str, Any]:
    pending = _collab_pending(snapshot.pending_ui_requests[-1] if snapshot.pending_ui_requests else None)
    state = snapshot.state
    capabilities = dict(CAPABILITIES["collab"])
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
    public_state = "disconnected" if snapshot.transport_status != "connected" else (
        "needs_input" if pending is not None else "working" if state.get("isStreaming") else "idle")
    return {"ompSessionId": snapshot.header.get("id") or session.get("ompSessionId"), "state": public_state,
            "transport": {"kind": "collab", "status": snapshot.transport_status,
                          "label": "Terminal session via Collab", "instanceId": instance_id, "reason": snapshot.reason},
            "capabilities": capabilities, "messages": normalize_messages(entries, session_id),
            "subagents": bounded_collection(snapshot.agents), "pendingUiRequest": pending, "updatedAt": now(),
            "workingOn": session.get("workingOn") if state.get("isStreaming") else None,
            "model": _model_identity(state), "thinking": state.get("thinkingLevel"), "_generation": generation,
            "_adapterSequence": snapshot.sequence, "_resyncRequired": False, "_resyncWatermark": 0}


def extension_projection(session: Mapping[str, Any], registration: Any, snapshot: Any,
                         instance_id: str, generation: int) -> dict[str, Any]:
    capabilities = dict(CAPABILITIES["extension"])
    for capability in capabilities:
        if capability in registration.capabilities:
            capabilities[capability] = capabilities[capability] and bool(registration.capabilities[capability])
    async_jobs = snapshot.async_jobs
    if isinstance(async_jobs, Mapping):
        async_jobs = async_jobs.get("jobs", [])
    if not isinstance(async_jobs, Sequence) or isinstance(async_jobs, (str, bytes)):
        async_jobs = []
    subagents = [item for item in async_jobs if isinstance(item, Mapping)]
    public_state = "disconnected" if snapshot.transport_status != "connected" else "idle" if snapshot.idle else "working"
    return {"ompSessionId": registration.session_id, "title": registration.title or session["title"],
            "project": {"name": session["project"]["name"], "path": registration.project}, "state": public_state,
            "transport": {"kind": "extension", "status": snapshot.transport_status,
                          "label": "Terminal session via extension", "instanceId": instance_id, "reason": snapshot.reason},
            "capabilities": capabilities, "subagents": bounded_collection(subagents), "updatedAt": now(),
            "model": _model_identity({"model": snapshot.model}), "_generation": generation,
            "_adapterSequence": snapshot.sequence, "_resyncRequired": False, "_resyncWatermark": 0}
