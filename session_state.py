import re


def content_items(message):
    content = message.get("content", [])
    return content if isinstance(content, list) else []


def message_text(message):
    content = message.get("content", [])
    if isinstance(content, str):
        return content.strip()
    parts = [
        item["text"]
        for item in content_items(message)
        if item.get("type") == "text" and item.get("text")
    ]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(parts)).strip()

def execution_state(status, connected=True):
    """Map the compatibility status onto the public execution-state vocabulary."""
    if not connected:
        return "disconnected"
    if status == "needs-input":
        return "needs-input"
    if status == "attention":
        return "failed"
    if status == "working":
        return "working"
    return "idle"


def session_state(pending_tools, latest_error, latest_message, last_tool_intent, shorten):
    waiting_tool = pending_tools[next(reversed(pending_tools))] if pending_tools else None
    role = latest_message.get("role") if latest_message else None
    stop_reason = latest_message.get("stopReason") if latest_message else None
    tool_name = latest_message.get("toolName") if latest_message else None
    if latest_error:
        status = "attention"
    elif role == "toolResult" and tool_name == "yield":
        status = "idle"
    elif role == "assistant" and stop_reason != "toolUse":
        status = "idle"
    elif waiting_tool and waiting_tool.get("toolName") == "ask":
        status = "needs-input"
    elif pending_tools:
        status = "working"
    elif latest_message:
        if role in {"user", "toolResult"} or stop_reason == "toolUse":
            status = "working"
        else:
            status = "idle"
    else:
        status = "idle"

    working_on = None
    if status in {"working", "attention"}:
        if waiting_tool:
            working_on = waiting_tool.get("intent")
        elif role == "user":
            working_on = "Preparing a response"
        else:
            working_on = last_tool_intent
        if not working_on:
            working_on = "Recovering from an error" if status == "attention" else "Preparing a response"
        working_on = shorten(working_on, 120)
    return status, working_on
