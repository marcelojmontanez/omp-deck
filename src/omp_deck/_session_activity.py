from collections.abc import Mapping, Sequence
from typing import Any

_MAX_ACTIVE_TOOLS = 512


def completion_state(messages: Sequence[Mapping[str, Any]] | None) -> str:
    for message in reversed(messages or ()):
        role = message.get("role")
        if role == "assistant":
            return "failed" if message.get("stopReason") == "error" or message.get("errorMessage") else "idle"
        if role == "toolResult":
            return "failed" if message.get("isError") else "idle"
        if role == "user":
            return "idle"
    return "idle"


def clear_activity(session: dict[str, Any]) -> None:
    session.pop("_activeTools", None)
    session.pop("_toolActivityOverflow", None)
    session["workingOn"] = None


def update_tool_activity(
    session: dict[str, Any], phase: str, tool_id: object, tool_name: object, intent: object = None
) -> None:
    tools = session.setdefault("_activeTools", {})
    if isinstance(tool_id, str) and tool_id:
        if phase == "end":
            tools.pop(tool_id, None)
        elif tool_id in tools or len(tools) < _MAX_ACTIVE_TOOLS:
            description = intent if isinstance(intent, str) and intent.strip() else None
            tools[tool_id] = description or tools.get(tool_id) or str(tool_name or "Using a tool")
        else:
            # Keep lifecycle truth without retaining unbounded tool descriptions.
            session["_toolActivityOverflow"] = True
    if session.get("_toolActivityOverflow"):
        session["workingOn"] = "Tool activity detail unavailable: too many active tools"
    else:
        session["workingOn"] = tools[next(reversed(tools))] if tools else None
