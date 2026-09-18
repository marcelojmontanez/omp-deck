from collections.abc import Mapping
from typing import Any

from ._fleet_projection import (
    bounded_collection,
    bounded_messages,
    message_identity,
    normalize_messages,
    pending_ui_projection,
    todos_from_tool_result,
)
from ._session_activity import clear_activity, completion_state, update_tool_activity
from .rpc_adapter import AdapterEvent


def _project_message(session: dict[str, Any], session_id: str, phase: str, raw: object) -> set[str]:
    if not isinstance(raw, Mapping):
        return set()
    changed = {"streamingMessage"}
    index = session.get("_rpcStreamingIndex")
    if phase == "start" or index is None:
        index = session.get("_rpcMessageCount", len(session["messages"]))
        # A snapshot may settle a message before its callback is applied. Match
        # native identity, never changing text or callback arrival timing.
        identity = message_identity(raw)
        if phase != "start" and identity is not None and identity == session.get("_rpcLastMessageKey"):
            index = max(0, index - 1)
            last_id = session.get("_rpcLastMessageId")
            session["messages"] = [message for message in session["messages"] if message["id"] != last_id]
            changed.add("messages")
        session["_rpcStreamingIndex"] = index
    session["_rpcStreamingKey"] = message_identity(raw)
    if phase == "end":
        normalized = normalize_messages([raw], session_id, offset=index)
        session["messages"] = bounded_messages([*session["messages"], *normalized])
        session["streamingMessage"] = None
        session["_rpcMessageCount"] = index + 1
        session["_rpcLastMessageKey"] = message_identity(raw)
        session["_rpcLastMessageId"] = normalized[-1]["id"] if normalized else None
        session["_rpcStreamingIndex"] = None
        session["_rpcStreamingKey"] = None
        changed.add("messages")
    else:
        stream_id = f"stream:{session['_generation']}:{index}"
        normalized = normalize_messages([{**raw, "id": stream_id}], session_id, offset=index)
        session["streamingMessage"] = normalized[0] if normalized else None
    return changed


def project_rpc_event(session: dict[str, Any], session_id: str, event: AdapterEvent) -> set[str]:
    """Apply one ordered native event; return the normalized fields consumers need."""
    kind, payload = event.type, event.payload
    changed = {"updatedAt"}
    if kind == "transport":
        status = payload.get("status", "disconnected")
        session["transport"].update(status=status, reason=payload.get("reason"))
        changed.add("transport")
        if status != "connected":
            session["state"] = "degraded" if status == "degraded" else "disconnected"
            session["pendingUiRequest"] = None
            session["streamingMessage"] = None
            session["subagents"] = []
            session["subagentError"] = None
            clear_activity(session)
            changed.update(("state", "pendingUiRequest", "streamingMessage", "subagents", "subagentError", "workingOn"))
    elif kind == "ui_request":
        pending = pending_ui_projection(payload)
        if pending is not None:
            session["pendingUiRequest"] = pending
            session["state"] = "needs_input"
            changed.update(("pendingUiRequest", "state"))
    elif kind in {"ui_request_resolved", "ui_request_cancelled", "ui_request_timeout"}:
        pending = session.get("pendingUiRequest")
        if isinstance(pending, Mapping) and pending.get("id") == payload.get("id"):
            session["pendingUiRequest"] = None
            session["state"] = "working"
            changed.update(("pendingUiRequest", "state"))
    elif kind == "agent_start":
        clear_activity(session)
        session["state"] = "working"
        changed.update(("state", "workingOn"))
    elif kind in {"turn_start", "message_start", "message_update", "message_end"}:
        if session.get("pendingUiRequest") is None:
            session["state"] = "working"
            changed.add("state")
        if kind.startswith("message_"):
            changed.update(_project_message(session, session_id, kind.removeprefix("message_"), payload.get("message")))
    elif kind == "agent_end" and payload.get("is_terminal") is not False:
        session["state"] = completion_state(payload.get("messages", ()))
        session["streamingMessage"] = None
        session["_rpcStreamingIndex"] = None
        session["_rpcStreamingKey"] = None
        clear_activity(session)
        changed.update(("state", "workingOn", "streamingMessage"))
    elif kind in {"tool_execution_start", "tool_execution_update", "tool_execution_end"}:
        phase = kind.removeprefix("tool_execution_")
        update_tool_activity(
            session, phase, payload.get("tool_call_id"), payload.get("tool_name"), payload.get("intent")
        )
        changed.add("workingOn")
        if phase == "end":
            todos = todos_from_tool_result(payload.get("tool_name"), payload.get("result"))
            if todos is not None:
                session["todos"] = todos
                changed.add("todos")
        elif session.get("pendingUiRequest") is None:
            session["state"] = "working"
            changed.add("state")
    elif kind == "subagents":
        value = payload.get("subagents")
        if isinstance(value, list):
            session["subagents"] = bounded_collection(value)
            session["subagentError"] = None
            changed.update(("subagents", "subagentError"))
    elif kind == "subagent_refresh_failed":
        session["subagentError"] = "Subagent updates unavailable. Waiting for the next native update."
        changed.add("subagentError")
    elif kind == "native_delivery_failed":
        # The official client exposes the failed native ID, but not successful
        # command IDs. Preserve that limitation instead of accusing another receipt.
        session["deliveryError"] = {
            "nativeRequestId": payload.get("nativeRequestId"),
            "errorCode": payload.get("errorCode", "command"),
            "correlationAvailable": False,
            "observedAt": session["updatedAt"],
        }
        changed.add("deliveryError")
    return changed
