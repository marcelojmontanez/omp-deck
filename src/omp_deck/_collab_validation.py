"""Private validators for published pi-wire v3 frame shapes."""

from __future__ import annotations

import math
from typing import Any, Callable

GUEST_FRAME_TYPES = frozenset(("hello", "prompt", "ui-response", "abort", "agent-cmd", "fetch-transcript"))
HOST_FRAME_TYPES = frozenset(("welcome", "snapshot-chunk", "entry", "event", "state", "bus", "agents", "ui-request", "ui-request-end", "transcript", "bye", "error"))

def _fail(message: str = "malformed collab frame") -> None:
    raise ValueError(message)


def _obj(value: Any, required: set[str], optional: set[str] | None = None) -> dict[str, Any]:
    optional = optional or set()
    if not isinstance(value, dict) or set(value) - required - optional or not required <= set(value):
        _fail()
    return value


def _str(value: Any) -> None:
    if not isinstance(value, str):
        _fail()


def _bool(value: Any) -> None:
    if not isinstance(value, bool):
        _fail()


def _num(value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        _fail()


def _uint(value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _fail()


def _literal(value: Any, choices: set[str]) -> None:
    if not isinstance(value, str) or value not in choices:
        _fail()


def _array(value: Any, check: Callable[[Any], None]) -> None:
    if not isinstance(value, list):
        _fail()
    for item in value:
        check(item)


def _json(value: Any) -> None:
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        _num(value)
        return
    if isinstance(value, list):
        _array(value, _json)
        return
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        for item in value.values():
            _json(item)
        return
    _fail()


def _content(value: Any, *, assistant: bool = False) -> None:
    if not isinstance(value, dict) or not isinstance(value.get("type"), str):
        _fail()
    kind = value["type"]
    if kind == "text":
        item = _obj(value, {"type", "text"}); _str(item["text"])
    elif kind == "image" and not assistant:
        item = _obj(value, {"type", "data", "mimeType"}); _str(item["data"]); _str(item["mimeType"])
    elif kind == "thinking" and assistant:
        item = _obj(value, {"type", "thinking"}); _str(item["thinking"])
    elif kind == "redactedThinking" and assistant:
        item = _obj(value, {"type", "data"}); _str(item["data"])
    elif kind == "toolCall" and assistant:
        item = _obj(value, {"type", "id", "name", "arguments"}, {"intent"})
        _str(item["id"]); _str(item["name"]); _obj(item["arguments"], set(), set(item["arguments"]) if isinstance(item["arguments"], dict) else set()); _json(item["arguments"])
        if "intent" in item: _str(item["intent"])
    else:
        _fail()


def _message(value: Any) -> None:
    if not isinstance(value, dict):
        _fail()
    role = value.get("role")
    if role in ("user", "developer"):
        optional = {"synthetic"} if role == "user" else set()
        item = _obj(value, {"role", "content", "timestamp"}, optional)
        content = item["content"]
        if isinstance(content, str): pass
        else: _array(content, _content)
        if "synthetic" in item: _bool(item["synthetic"])
        _num(item["timestamp"])
    elif role == "assistant":
        item = _obj(value, {"role", "content", "model", "usage", "stopReason", "timestamp"}, {"errorMessage"})
        _array(item["content"], lambda x: _content(x, assistant=True)); _str(item["model"])
        usage = _obj(item["usage"], {"input", "output", "cacheRead", "cacheWrite", "totalTokens", "cost"})
        for key in ("input", "output", "cacheRead", "cacheWrite", "totalTokens"): _num(usage[key])
        cost = _obj(usage["cost"], {"total"}); _num(cost["total"])
        _literal(item["stopReason"], {"stop", "length", "toolUse", "error", "aborted"}); _num(item["timestamp"])
        if "errorMessage" in item: _str(item["errorMessage"])
    elif role == "toolResult":
        item = _obj(value, {"role", "toolCallId", "toolName", "content", "isError", "timestamp"}, {"details"})
        _str(item["toolCallId"]); _str(item["toolName"]); _array(item["content"], _content); _bool(item["isError"]); _num(item["timestamp"])
        if "details" in item: _json(item["details"])
    else:
        _fail()


def _entry(value: Any) -> None:
    if not isinstance(value, dict) or not isinstance(value.get("type"), str):
        _fail()
    kind = value["type"]
    schemas = {
        "message": ({"type", "id", "parentId", "timestamp", "message"}, set()),
        "custom_message": ({"type", "id", "parentId", "timestamp", "customType", "content", "display"}, {"details"}),
        "compaction": ({"type", "id", "parentId", "timestamp", "summary", "firstKeptEntryId", "tokensBefore"}, {"shortSummary"}),
        "branch_summary": ({"type", "id", "parentId", "timestamp", "fromId", "summary"}, set()),
        "model_change": ({"type", "id", "parentId", "timestamp", "model"}, {"role"}),
        "thinking_level_change": ({"type", "id", "parentId", "timestamp"}, {"thinkingLevel"}),
    }
    if kind not in schemas:
        _json(value)
        return
    item = _obj(value, *schemas[kind]); _str(item["id"]); _str(item["timestamp"])
    if item["parentId"] is not None: _str(item["parentId"])
    if kind == "message": _message(item["message"])
    elif kind == "custom_message":
        _str(item["customType"]); _bool(item["display"])
        if isinstance(item["content"], str): pass
        else: _array(item["content"], _content)
        if "details" in item: _json(item["details"])
    elif kind == "compaction":
        _str(item["summary"]); _str(item["firstKeptEntryId"]); _num(item["tokensBefore"])
        if "shortSummary" in item: _str(item["shortSummary"])
    elif kind == "branch_summary": _str(item["fromId"]); _str(item["summary"])
    elif kind == "model_change":
        _str(item["model"])
        if "role" in item: _str(item["role"])
    elif "thinkingLevel" in item and item["thinkingLevel"] is not None: _str(item["thinkingLevel"])


def _participant(value: Any) -> None:
    item = _obj(value, {"name", "role"}, {"readOnly"}); _str(item["name"]); _literal(item["role"], {"host", "guest"})
    if "readOnly" in item: _bool(item["readOnly"])


def _state(value: Any) -> None:
    item = _obj(value, {"isStreaming", "queuedMessageCount", "cwd", "participants"}, {"sessionName", "model", "thinkingLevel", "contextUsage", "isAborting"})
    _bool(item["isStreaming"]); _uint(item["queuedMessageCount"]); _str(item["cwd"]); _array(item["participants"], _participant)
    for key in ("sessionName", "thinkingLevel"):
        if key in item: _str(item[key])
    for key in ("isAborting",):
        if key in item: _bool(item[key])
    if "model" in item:
        model = _obj(item["model"], {"id", "name", "provider", "contextWindow"})
        for key in ("id", "name", "provider"): _str(model[key])
        if model["contextWindow"] is not None: _uint(model["contextWindow"])
    if "contextUsage" in item:
        context = _obj(item["contextUsage"], {"tokens", "contextWindow", "percent"})
        for key in context:
            if context[key] is not None: _num(context[key])


def _agent(value: Any) -> None:
    item = _obj(value, {"id", "displayName", "kind", "status", "hasSessionFile", "createdAt", "lastActivity"}, {"parentId"})
    _str(item["id"]); _str(item["displayName"]); _literal(item["kind"], {"main", "sub"})
    _literal(item["status"], {"running", "idle", "parked", "aborted"}); _bool(item["hasSessionFile"]); _num(item["createdAt"]); _num(item["lastActivity"])
    if "parentId" in item: _str(item["parentId"])


def _event(value: Any) -> None:
    if not isinstance(value, dict) or not isinstance(value.get("type"), str): _fail()
    kind = value["type"]
    empty = {"agent_start", "agent_end", "turn_start", "turn_end"}
    if kind in empty: _obj(value, {"type"}); return
    if kind not in {"message_start", "message_update", "message_end", "tool_execution_start", "tool_execution_update", "tool_execution_end", "notice", "auto_compaction_start", "auto_compaction_end", "auto_retry_start", "auto_retry_end", "thinking_level_changed"}:
        _json(value); return
    specs = {
        "message_start": ({"type", "message"}, set()), "message_update": ({"type", "message"}, set()), "message_end": ({"type", "message"}, set()),
        "tool_execution_start": ({"type", "toolCallId", "toolName", "args"}, {"intent"}),
        "tool_execution_update": ({"type", "toolCallId", "toolName", "args", "partialResult"}, set()),
        "tool_execution_end": ({"type", "toolCallId", "toolName", "result"}, {"isError"}),
        "notice": ({"type", "level", "message"}, {"source"}),
        "auto_compaction_start": ({"type", "reason", "action"}, set()),
        "auto_compaction_end": ({"type", "aborted", "willRetry"}, {"errorMessage", "skipped"}),
        "auto_retry_start": ({"type", "attempt", "maxAttempts", "delayMs", "errorMessage"}, set()),
        "auto_retry_end": ({"type", "success", "attempt"}, {"finalError"}),
        "thinking_level_changed": ({"type"}, {"thinkingLevel"}),
    }
    item = _obj(value, *specs[kind])
    if kind.startswith("message_"): _message(item["message"])
    elif kind.startswith("tool_execution_"):
        _str(item["toolCallId"]); _str(item["toolName"])
        for key in ("args", "partialResult", "result"):
            if key in item: _json(item[key])
        if "intent" in item: _str(item["intent"])
        if "isError" in item: _bool(item["isError"])
    elif kind == "notice":
        _literal(item["level"], {"info", "warning", "error"}); _str(item["message"])
        if "source" in item: _str(item["source"])
    elif kind == "auto_compaction_start": _str(item["reason"]); _str(item["action"])
    elif kind == "auto_compaction_end":
        _bool(item["aborted"]); _bool(item["willRetry"])
        if "errorMessage" in item: _str(item["errorMessage"])
        if "skipped" in item: _bool(item["skipped"])
    elif kind == "auto_retry_start":
        _uint(item["attempt"]); _uint(item["maxAttempts"]); _uint(item["delayMs"]); _str(item["errorMessage"])
    elif kind == "auto_retry_end":
        _bool(item["success"]); _uint(item["attempt"])
        if "finalError" in item: _str(item["finalError"])
    elif "thinkingLevel" in item: _str(item["thinkingLevel"])


def _ui_request(value: Any) -> None:
    if not isinstance(value, dict): _fail()
    if value.get("kind") == "select":
        item = _obj(value, {"kind", "title", "options", "reqId"}, {"initialIndex", "selectionMarker", "checkedIndices", "markableCount", "helpText"})
        def option(v: Any) -> None:
            if isinstance(v, str): return
            option_obj = _obj(v, {"label"}, {"description"}); _str(option_obj["label"])
            if "description" in option_obj: _str(option_obj["description"])
        _array(item["options"], option)
        for key in ("initialIndex", "markableCount"):
            if key in item: _uint(item[key])
        if "checkedIndices" in item: _array(item["checkedIndices"], _uint)
        if "selectionMarker" in item: _literal(item["selectionMarker"], {"radio", "checkbox"})
        if "helpText" in item: _str(item["helpText"])
    elif value.get("kind") == "editor":
        item = _obj(value, {"kind", "title", "reqId"}, {"prefill"})
        if "prefill" in item: _str(item["prefill"])
    else: _fail()
    _str(item["title"]); _uint(item["reqId"])


def _validate_guest_frame(frame: dict[str, Any], frame_type: str) -> None:
    if frame_type == "hello":
        item = _obj(frame, {"t", "proto", "name"}, {"writeToken"}); _uint(item["proto"]); _str(item["name"])
        if "writeToken" in item: _str(item["writeToken"])
    elif frame_type == "prompt":
        item = _obj(frame, {"t", "text"}, {"images"}); _str(item["text"])
        if "images" in item: _array(item["images"], _content)
    elif frame_type == "ui-response":
        item = _obj(frame, {"t", "reqId"}, {"value"}); _uint(item["reqId"])
        if "value" in item: _str(item["value"])
    elif frame_type == "abort":
        _obj(frame, {"t"})
    elif frame_type == "agent-cmd":
        item = _obj(frame, {"t", "cmd", "agentId"}, {"text"}); _literal(item["cmd"], {"chat", "kill", "revive"}); _str(item["agentId"])
        if "text" in item: _str(item["text"])
    else:
        item = _obj(frame, {"t", "reqId", "agentId", "fromByte"}); _uint(item["reqId"]); _str(item["agentId"]); _uint(item["fromByte"])


def _validate_host_frame(frame: dict[str, Any], frame_type: str) -> None:
    if frame_type == "welcome":
        item = _obj(frame, {"t", "proto", "header", "state", "agents", "entryCount"}, {"readOnly"}); _uint(item["proto"]); _state(item["state"]); _array(item["agents"], _agent); _uint(item["entryCount"])
        header = _obj(item["header"], {"type", "id", "timestamp", "cwd"}, {"title"}); _literal(header["type"], {"session"})
        for key in ("id", "timestamp", "cwd"): _str(header[key])
        if "title" in header: _str(header["title"])
        if "readOnly" in item: _bool(item["readOnly"])
    elif frame_type == "snapshot-chunk":
        item = _obj(frame, {"t", "entries", "final"}); _array(item["entries"], _entry); _bool(item["final"])
    elif frame_type == "entry":
        item = _obj(frame, {"t", "entry"}); _entry(item["entry"])
    elif frame_type == "event":
        item = _obj(frame, {"t", "event"}); _event(item["event"])
    elif frame_type == "state":
        item = _obj(frame, {"t", "state"}); _state(item["state"])
    elif frame_type == "bus":
        item = _obj(frame, {"t", "channel", "data"}); _literal(item["channel"], {"task:subagent:progress", "task:subagent:lifecycle"}); _json(item["data"])
    elif frame_type == "agents":
        item = _obj(frame, {"t", "agents"}); _array(item["agents"], _agent)
    elif frame_type == "ui-request":
        item = _obj(frame, {"t", "request"}); _ui_request(item["request"])
    elif frame_type == "ui-request-end":
        item = _obj(frame, {"t", "reqId"}); _uint(item["reqId"])
    elif frame_type == "transcript":
        item = _obj(frame, {"t", "reqId", "text", "newSize"}, {"error"}); _uint(item["reqId"]); _str(item["text"]); _uint(item["newSize"])
        if "error" in item: _str(item["error"])
    elif frame_type == "bye":
        item = _obj(frame, {"t", "reason"}); _str(item["reason"])
    else:
        item = _obj(frame, {"t", "message"}); _str(item["message"])


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
