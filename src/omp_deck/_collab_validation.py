"""Private validators for published pi-wire v3 frame shapes."""

from __future__ import annotations

from typing import Any

from ._collab_values import _array, _bool, _content, _fail, _json, _literal, _message, _num, _obj, _str, _uint

GUEST_FRAME_TYPES = frozenset(("hello", "prompt", "ui-response", "abort", "agent-cmd", "fetch-transcript"))
HOST_FRAME_TYPES = frozenset(
    (
        "welcome",
        "snapshot-chunk",
        "entry",
        "event",
        "state",
        "bus",
        "agents",
        "ui-request",
        "ui-request-end",
        "transcript",
        "bye",
        "error",
    )
)


def _entry(value: Any) -> None:
    if not isinstance(value, dict) or not isinstance(value.get("type"), str):
        _fail()
    kind = value["type"]
    schemas = {
        "message": ({"type", "id", "parentId", "timestamp", "message"}, set()),
        "custom_message": ({"type", "id", "parentId", "timestamp", "customType", "content", "display"}, {"details"}),
        "compaction": (
            {"type", "id", "parentId", "timestamp", "summary", "firstKeptEntryId", "tokensBefore"},
            {"shortSummary"},
        ),
        "branch_summary": ({"type", "id", "parentId", "timestamp", "fromId", "summary"}, set()),
        "model_change": ({"type", "id", "parentId", "timestamp", "model"}, {"role"}),
        "thinking_level_change": ({"type", "id", "parentId", "timestamp"}, {"thinkingLevel"}),
    }
    if kind not in schemas:
        _json(value)
        return
    item = _obj(value, *schemas[kind])
    _str(item["id"])
    _str(item["timestamp"])
    if item["parentId"] is not None:
        _str(item["parentId"])
    if kind == "message":
        _message(item["message"])
    elif kind == "custom_message":
        _str(item["customType"])
        _bool(item["display"])
        if isinstance(item["content"], str):
            pass
        else:
            _array(item["content"], _content)
        if "details" in item:
            _json(item["details"])
    elif kind == "compaction":
        _str(item["summary"])
        _str(item["firstKeptEntryId"])
        _num(item["tokensBefore"])
        if "shortSummary" in item:
            _str(item["shortSummary"])
    elif kind == "branch_summary":
        _str(item["fromId"])
        _str(item["summary"])
    elif kind == "model_change":
        _str(item["model"])
        if "role" in item:
            _str(item["role"])
    elif "thinkingLevel" in item and item["thinkingLevel"] is not None:
        _str(item["thinkingLevel"])


def _participant(value: Any) -> None:
    item = _obj(value, {"name", "role"}, {"readOnly"})
    _str(item["name"])
    _literal(item["role"], {"host", "guest"})
    if "readOnly" in item:
        _bool(item["readOnly"])


def _state(value: Any) -> None:
    item = _obj(
        value,
        {"isStreaming", "queuedMessageCount", "cwd", "participants"},
        {"sessionName", "model", "thinkingLevel", "contextUsage", "isAborting"},
    )
    _bool(item["isStreaming"])
    _uint(item["queuedMessageCount"])
    _str(item["cwd"])
    _array(item["participants"], _participant)
    for key in ("sessionName", "thinkingLevel"):
        if key in item:
            _str(item[key])
    for key in ("isAborting",):
        if key in item:
            _bool(item[key])
    if "model" in item:
        model = _obj(
            item["model"],
            {"id", "name", "provider", "contextWindow"},
            set(item["model"]) - {"id", "name", "provider", "contextWindow"}
            if isinstance(item["model"], dict)
            else set(),
        )
        for key in ("id", "name", "provider"):
            _str(model[key])
        if model["contextWindow"] is not None:
            _uint(model["contextWindow"])
        for key in set(model) - {"id", "name", "provider", "contextWindow"}:
            _json(model[key])
    if "contextUsage" in item:
        context = _obj(item["contextUsage"], {"tokens", "contextWindow", "percent"})
        for key in context:
            if context[key] is not None:
                _num(context[key])


def _agent(value: Any) -> None:
    item = _obj(
        value, {"id", "displayName", "kind", "status", "hasSessionFile", "createdAt", "lastActivity"}, {"parentId"}
    )
    _str(item["id"])
    _str(item["displayName"])
    _literal(item["kind"], {"main", "sub"})
    _literal(item["status"], {"running", "idle", "parked", "aborted"})
    _bool(item["hasSessionFile"])
    _num(item["createdAt"])
    _num(item["lastActivity"])
    if "parentId" in item:
        _str(item["parentId"])


def _agent_end_event(value: dict[str, Any]) -> None:
    item = _obj(value, {"type"}, {"messages", "telemetry", "coverage", "isTerminal"})
    if "messages" in item:
        _array(item["messages"], _message)
    for key in ("telemetry", "coverage"):
        if key in item:
            _json(item[key])
    if "isTerminal" in item:
        _bool(item["isTerminal"])


def _turn_end_event(value: dict[str, Any]) -> None:
    item = _obj(value, {"type"}, {"message", "toolResults"})
    if "message" in item:
        _message(item["message"])
    if "toolResults" in item:
        _array(item["toolResults"], _message)


def _message_event(item: dict[str, Any]) -> None:
    _message(item["message"])
    if "assistantMessageEvent" in item:
        _json(item["assistantMessageEvent"])


def _tool_execution_event(item: dict[str, Any]) -> None:
    _str(item["toolCallId"])
    _str(item["toolName"])
    for key in ("args", "partialResult", "result"):
        if key in item:
            _json(item[key])
    if "intent" in item:
        _str(item["intent"])
    if "isError" in item:
        _bool(item["isError"])


def _notice_event(item: dict[str, Any]) -> None:
    _literal(item["level"], {"info", "warning", "error"})
    _str(item["message"])
    if "source" in item:
        _str(item["source"])


def _compaction_start_event(item: dict[str, Any]) -> None:
    _str(item["reason"])
    _str(item["action"])


def _compaction_end_event(item: dict[str, Any]) -> None:
    _bool(item["aborted"])
    _bool(item["willRetry"])
    if "errorMessage" in item:
        _str(item["errorMessage"])
    if "skipped" in item:
        _bool(item["skipped"])
    for key in ("action", "result"):
        if key in item:
            _json(item[key])


def _retry_start_event(item: dict[str, Any]) -> None:
    _uint(item["attempt"])
    _uint(item["maxAttempts"])
    _uint(item["delayMs"])
    _str(item["errorMessage"])
    if "errorId" in item:
        _uint(item["errorId"])


def _retry_end_event(item: dict[str, Any]) -> None:
    _bool(item["success"])
    _uint(item["attempt"])
    if "finalError" in item:
        _str(item["finalError"])
    if "retryErrors" in item:
        _json(item["retryErrors"])


def _thinking_level_event(item: dict[str, Any]) -> None:
    for key in ("thinkingLevel", "configured", "resolved"):
        if key in item:
            _str(item[key])


_EVENT_SPECS = {
    "message_start": ({"type", "message"}, set()),
    "message_update": ({"type", "message"}, {"assistantMessageEvent"}),
    "message_end": ({"type", "message"}, set()),
    "tool_execution_start": ({"type", "toolCallId", "toolName", "args"}, {"intent"}),
    "tool_execution_update": ({"type", "toolCallId", "toolName", "args", "partialResult"}, set()),
    "tool_execution_end": ({"type", "toolCallId", "toolName", "result"}, {"isError"}),
    "notice": ({"type", "level", "message"}, {"source"}),
    "auto_compaction_start": ({"type", "reason", "action"}, set()),
    "auto_compaction_end": ({"type", "aborted", "willRetry"}, {"action", "result", "errorMessage", "skipped"}),
    "auto_retry_start": ({"type", "attempt", "maxAttempts", "delayMs", "errorMessage"}, {"errorId"}),
    "auto_retry_end": ({"type", "success", "attempt"}, {"finalError", "retryErrors"}),
    "thinking_level_changed": ({"type"}, {"thinkingLevel", "configured", "resolved"}),
}
_EVENT_VALIDATORS = {
    "message_start": _message_event,
    "message_update": _message_event,
    "message_end": _message_event,
    "tool_execution_start": _tool_execution_event,
    "tool_execution_update": _tool_execution_event,
    "tool_execution_end": _tool_execution_event,
    "notice": _notice_event,
    "auto_compaction_start": _compaction_start_event,
    "auto_compaction_end": _compaction_end_event,
    "auto_retry_start": _retry_start_event,
    "auto_retry_end": _retry_end_event,
    "thinking_level_changed": _thinking_level_event,
}


def _event(value: Any) -> None:
    if not isinstance(value, dict) or not isinstance(value.get("type"), str):
        _fail()
    kind = value["type"]
    if kind in {"agent_start", "turn_start"}:
        _obj(value, {"type"})
    elif kind == "agent_end":
        _agent_end_event(value)
    elif kind == "turn_end":
        _turn_end_event(value)
    elif kind in _EVENT_SPECS:
        _EVENT_VALIDATORS[kind](_obj(value, *_EVENT_SPECS[kind]))
    else:
        _json(value)


def _ui_request(value: Any) -> None:
    if not isinstance(value, dict):
        _fail()
    if value.get("kind") == "select":
        item = _obj(
            value,
            {"kind", "title", "options", "reqId"},
            {"initialIndex", "selectionMarker", "checkedIndices", "markableCount", "helpText"},
        )

        def option(v: Any) -> None:
            if isinstance(v, str):
                return
            option_obj = _obj(v, {"label"}, {"description"})
            _str(option_obj["label"])
            if "description" in option_obj:
                _str(option_obj["description"])

        _array(item["options"], option)
        for key in ("initialIndex", "markableCount"):
            if key in item:
                _uint(item[key])
        if "checkedIndices" in item:
            _array(item["checkedIndices"], _uint)
        if "selectionMarker" in item:
            _literal(item["selectionMarker"], {"radio", "checkbox"})
        if "helpText" in item:
            _str(item["helpText"])
    elif value.get("kind") == "editor":
        item = _obj(value, {"kind", "title", "reqId"}, {"prefill"})
        if "prefill" in item:
            _str(item["prefill"])
    else:
        _fail()
    _str(item["title"])
    _uint(item["reqId"])


def _validate_guest_frame(frame: dict[str, Any], frame_type: str) -> None:
    if frame_type == "hello":
        item = _obj(frame, {"t", "proto", "name"}, {"writeToken"})
        _uint(item["proto"])
        _str(item["name"])
        if "writeToken" in item:
            _str(item["writeToken"])
    elif frame_type == "prompt":
        item = _obj(frame, {"t", "text"}, {"images"})
        _str(item["text"])
        if "images" in item:
            _array(item["images"], _content)
    elif frame_type == "ui-response":
        item = _obj(frame, {"t", "reqId"}, {"value"})
        _uint(item["reqId"])
        if "value" in item:
            _str(item["value"])
    elif frame_type == "abort":
        _obj(frame, {"t"})
    elif frame_type == "agent-cmd":
        item = _obj(frame, {"t", "cmd", "agentId"}, {"text"})
        _literal(item["cmd"], {"chat", "kill", "revive"})
        _str(item["agentId"])
        if "text" in item:
            _str(item["text"])
    else:
        item = _obj(frame, {"t", "reqId", "agentId", "fromByte"})
        _uint(item["reqId"])
        _str(item["agentId"])
        _uint(item["fromByte"])


def _validate_host_frame(frame: dict[str, Any], frame_type: str) -> None:
    if frame_type == "welcome":
        item = _obj(frame, {"t", "proto", "header", "state", "agents", "entryCount"}, {"readOnly"})
        _uint(item["proto"])
        _state(item["state"])
        _array(item["agents"], _agent)
        _uint(item["entryCount"])
        header = _obj(
            item["header"],
            {"type", "id", "timestamp", "cwd"},
            {
                "version",
                "title",
                "titleSource",
                "additionalDirectories",
                "parentSession",
                "previousSessionFiles",
                "providerPromptCacheKey",
            },
        )
        _literal(header["type"], {"session"})
        for key in ("id", "timestamp", "cwd"):
            _str(header[key])
        if "version" in header:
            _uint(header["version"])
        for key in ("title", "titleSource", "parentSession", "providerPromptCacheKey"):
            if key in header:
                _str(header[key])
        for key in ("additionalDirectories", "previousSessionFiles"):
            if key in header:
                _array(header[key], _str)
        if "readOnly" in item:
            _bool(item["readOnly"])
    elif frame_type == "snapshot-chunk":
        item = _obj(frame, {"t", "entries", "final"})
        _array(item["entries"], _entry)
        _bool(item["final"])
    elif frame_type == "entry":
        item = _obj(frame, {"t", "entry"})
        _entry(item["entry"])
    elif frame_type == "event":
        item = _obj(frame, {"t", "event"})
        _event(item["event"])
    elif frame_type == "state":
        item = _obj(frame, {"t", "state"})
        _state(item["state"])
    elif frame_type == "bus":
        item = _obj(frame, {"t", "channel", "data"})
        _literal(item["channel"], {"task:subagent:progress", "task:subagent:lifecycle"})
        _json(item["data"])
    elif frame_type == "agents":
        item = _obj(frame, {"t", "agents"})
        _array(item["agents"], _agent)
    elif frame_type == "ui-request":
        item = _obj(frame, {"t", "request"})
        _ui_request(item["request"])
    elif frame_type == "ui-request-end":
        item = _obj(frame, {"t", "reqId"})
        _uint(item["reqId"])
    elif frame_type == "transcript":
        item = _obj(frame, {"t", "reqId", "text", "newSize"}, {"error"})
        _uint(item["reqId"])
        _str(item["text"])
        _uint(item["newSize"])
        if "error" in item:
            _str(item["error"])
    elif frame_type == "bye":
        item = _obj(frame, {"t", "reason"})
        _str(item["reason"])
    else:
        item = _obj(frame, {"t", "message"})
        _str(item["message"])


def validate_frame(frame: Any, *, direction: str | None = None) -> dict[str, Any]:
    if direction not in (None, "guest", "host"):
        raise ValueError("direction must be guest, host, or None")
    if not isinstance(frame, dict) or not isinstance(frame.get("t"), str):
        _fail()
    frame_type = frame["t"]
    if frame_type not in GUEST_FRAME_TYPES | HOST_FRAME_TYPES:
        raise ValueError("unknown collab frame type")
    if direction == "guest" and frame_type not in GUEST_FRAME_TYPES:
        raise ValueError("unknown collab frame type")
    if direction == "host" and frame_type not in HOST_FRAME_TYPES:
        raise ValueError("unknown collab frame type")
    if frame_type in GUEST_FRAME_TYPES:
        _validate_guest_frame(frame, frame_type)
    else:
        _validate_host_frame(frame, frame_type)
    return frame


def validate_guest_frame(frame: Any) -> dict[str, Any]:
    return validate_frame(frame, direction="guest")


def validate_host_frame(frame: Any) -> dict[str, Any]:
    return validate_frame(frame, direction="host")
