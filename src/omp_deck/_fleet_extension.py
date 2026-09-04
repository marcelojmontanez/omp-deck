from __future__ import annotations

from typing import Any, Mapping, Sequence

from ._fleet_projection import bounded_collection, normalize_messages


def project_extension_event(session: dict[str, Any], session_id: str, kind: str,
                            payload: Mapping[str, Any]) -> None:
    if kind == "message":
        _apply_message(session, session_id, payload)
    elif kind == "tool":
        _apply_tool(session, payload)
    elif kind == "lifecycle":
        lifecycle_state = payload.get("state")
        if lifecycle_state in {"agent_start", "session_start"}:
            session["state"] = "working"
        elif lifecycle_state in {"agent_end", "session_shutdown"}:
            session["state"] = "idle" if lifecycle_state == "agent_end" else "disconnected"
    elif kind == "todo" and isinstance(payload.get("todos"), Sequence):
        session["todos"] = bounded_collection([
            item for item in payload["todos"] if isinstance(item, Mapping)
        ])


def _apply_message(session: dict[str, Any], session_id: str, payload: Mapping[str, Any]) -> None:
    phase = payload.get("phase")
    message = payload.get("message")
    if phase == "end" and isinstance(message, Mapping):
        raw_messages = [{"id": item.get("id"), "role": item.get("role"),
                         "content": item.get("content"), "timestamp": item.get("createdAt")}
                        for item in session["messages"]]
        raw_messages.append(message)
        session["messages"] = normalize_messages(raw_messages, session_id)
        if message.get("role") == "assistant":
            session["unread"] = True
            session["state"] = "idle"
    elif phase in {"start", "update"}:
        session["state"] = "working"


def _apply_tool(session: dict[str, Any], payload: Mapping[str, Any]) -> None:
    phase = payload.get("phase")
    tool_name = payload.get("toolName")
    if phase == "start" and isinstance(tool_name, str) and tool_name.casefold() == "ask":
        session["pendingUiRequest"] = {
            "id": str(payload.get("toolCallId", "")), "kind": "terminal_only",
            "title": "Input required in terminal", "message": "Answer this request in the terminal",
            "questions": [], "questionIndex": 0, "allowCancel": False, "expiresAt": None,
            "status": "terminal_only",
        }
        session["state"] = "needs_input"
    elif phase == "start":
        session["workingOn"] = str(payload.get("intent") or tool_name or "Using a tool")
        session["state"] = "working"
    elif phase == "end":
        session["workingOn"] = None
        pending = session.get("pendingUiRequest")
        if isinstance(pending, Mapping) and pending.get("id") == str(payload.get("toolCallId", "")):
            session["pendingUiRequest"] = None
