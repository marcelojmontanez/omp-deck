from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from ._fleet_projection import (
    MAX_MESSAGE_BYTES,
    MAX_MESSAGES,
    has_visible_content,
    normalize_messages,
    normalize_todos,
    strict_clone,
)
from ._session_activity import clear_activity, completion_state, update_tool_activity

_HISTORY_SYNC_KEY = "_extensionHistorySync"


def _apply_history(session: dict[str, Any], session_id: str, payload: Mapping[str, Any]) -> None:
    sync_id = payload.get("syncId")
    offset = payload.get("offset")
    messages = payload.get("messages")
    complete = payload.get("complete")
    valid = (
        isinstance(sync_id, str)
        and bool(sync_id)
        and len(sync_id) <= 256
        and isinstance(offset, int)
        and not isinstance(offset, bool)
        and offset >= 0
        and isinstance(messages, Sequence)
        and not isinstance(messages, (str, bytes, bytearray))
        and isinstance(complete, bool)
    )
    if not valid:
        session.pop(_HISTORY_SYNC_KEY, None)
        return
    pending = session.get(_HISTORY_SYNC_KEY)
    if offset == 0:
        pending = {
            "syncId": sync_id,
            "nextOffset": 0,
            "messages": [],
            "sizes": [],
            "bytes": 0,
        }
        session[_HISTORY_SYNC_KEY] = pending
    if not isinstance(pending, dict) or pending.get("syncId") != sync_id or pending.get("nextOffset") != offset:
        session.pop(_HISTORY_SYNC_KEY, None)
        return
    retained = pending["messages"]
    sizes = pending["sizes"]
    for message in messages:
        if not isinstance(message, Mapping):
            session.pop(_HISTORY_SYNC_KEY, None)
            return
        try:
            cloned = strict_clone(message)
            size = len(json.dumps(cloned, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8"))
        except (TypeError, ValueError):
            session.pop(_HISTORY_SYNC_KEY, None)
            return
        retained.append(cloned)
        sizes.append(size)
        pending["bytes"] += size
        while len(retained) > MAX_MESSAGES or pending["bytes"] > MAX_MESSAGE_BYTES:
            retained.pop(0)
            pending["bytes"] -= sizes.pop(0)
    pending["nextOffset"] = offset + len(messages)
    if complete:
        session["messages"] = normalize_messages(retained, session_id)
        if session["state"] in {"idle", "failed"}:
            session["state"] = completion_state(retained)
        session["historyError"] = None
        session.pop(_HISTORY_SYNC_KEY, None)


def project_extension_event(session: dict[str, Any], session_id: str, kind: str, payload: Mapping[str, Any]) -> None:
    if kind == "message":
        _apply_message(session, session_id, payload)
    elif kind == "history":
        _apply_history(session, session_id, payload)
    elif kind == "tool":
        _apply_tool(session, payload)
    elif kind == "lifecycle":
        lifecycle_state = payload.get("state")
        if lifecycle_state == "agent_start":
            clear_activity(session)
            session["state"] = "working"
        elif lifecycle_state in {"session_start", "agent_end", "session_shutdown"}:
            clear_activity(session)
            session["pendingUiRequest"] = None
            if lifecycle_state == "session_shutdown":
                session["state"] = "disconnected"
            elif lifecycle_state != "agent_end" or session["state"] != "failed":
                session["state"] = "idle"
    elif kind == "degraded":
        reason = payload.get("reason")
        if not isinstance(reason, str) or not reason:
            reason = "extension event degraded"
        if payload.get("droppedKind") == "history":
            session.pop(_HISTORY_SYNC_KEY, None)
            session["historyError"] = reason[:256]
        else:
            session["state"] = "degraded"
    elif kind == "todo" and isinstance(payload.get("todos"), Sequence):
        session["todos"] = normalize_todos([item for item in payload["todos"] if isinstance(item, Mapping)])


def _apply_message(session: dict[str, Any], session_id: str, payload: Mapping[str, Any]) -> None:
    phase = payload.get("phase")
    message = payload.get("message")
    if phase == "end" and isinstance(message, Mapping):
        raw_messages = [
            {
                "id": item.get("id"),
                "role": item.get("role"),
                "content": item.get("content"),
                "timestamp": item.get("createdAt"),
            }
            for item in session["messages"]
        ]
        raw_messages.append(message)
        session["messages"] = normalize_messages(raw_messages, session_id)
        normalized = session["messages"][-1]
        if message.get("role") == "assistant":
            if has_visible_content(normalized):
                session["unread"] = True
            if completion_state([message]) == "failed":
                session["state"] = "failed"
    elif phase in {"start", "update"}:
        if session.get("pendingUiRequest") is None:
            session["state"] = "working"


def _apply_tool(session: dict[str, Any], payload: Mapping[str, Any]) -> None:
    phase = payload.get("phase")
    tool_name = payload.get("toolName")
    update_tool_activity(session, phase, payload.get("toolCallId"), tool_name, payload.get("intent"))
    if phase == "start" and isinstance(tool_name, str) and tool_name.casefold() == "ask":
        session["pendingUiRequest"] = {
            "id": str(payload.get("toolCallId", "")),
            "kind": "terminal_only",
            "title": "Input required in terminal",
            "message": "Answer this request in the terminal",
            "questions": [],
            "questionIndex": 0,
            "allowCancel": False,
            "expiresAt": None,
            "status": "terminal_only",
        }
        session["state"] = "needs_input"
    elif phase == "start":
        if session.get("pendingUiRequest") is None:
            session["state"] = "working"
    elif phase == "end":
        pending = session.get("pendingUiRequest")
        if isinstance(pending, Mapping) and pending.get("id") == str(payload.get("toolCallId", "")):
            session["pendingUiRequest"] = None
            session["state"] = "working"
