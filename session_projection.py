import json
import re
import time
from collections import deque
from datetime import datetime, timezone

from attachments import message_images
from session_state import content_items, execution_state, message_text, session_state

MAX_CHAT_MESSAGES = 200

def session_events(path):
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return

def shorten(value, limit=700):
    value = re.sub(r"\n{3,}", "\n\n", str(value or "")).strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"

def event_time(event):
    value = event.get("timestamp")
    if isinstance(value, str):
        return value
    message_value = event.get("message", {}).get("timestamp")
    if isinstance(message_value, (int, float)):
        return datetime.fromtimestamp(message_value / 1000, timezone.utc).isoformat()
    return None

def blank_session(header):
    registered_at = header.get("registeredAt") or datetime.now(timezone.utc).isoformat()
    try:
        updated_ms = int(datetime.fromisoformat(registered_at).timestamp() * 1000)
    except (TypeError, ValueError):
        updated_ms = int(time.time() * 1000)
    return {
        "sessionId": header.get("sessionId"),
        "model": header.get("model"),
        "thinking": header.get("thinking"),
        "status": "idle",
        "executionState": "idle",
        "inboxState": "read",
        "workingOn": None,
        "updatedAt": registered_at,
        "updatedMs": updated_ms,
        "preview": "",
        "activitySummary": "",
        "activity": [],
        "archiveActivity": [],
        "contextResetAt": None,
        "responseAt": None,
        "responseId": None,
        "todo": None,
        "eventIds": [],
        "inboxEventIds": [],
    }

def new_session_projection(header):
    return {
        "activity": deque(maxlen=MAX_CHAT_MESSAGES),
        "pendingTools": {},
        "latestMessage": None,
        "lastAssistant": "",
        "model": header.get("model"),
        "thinking": header.get("thinking"),
        "latestError": False,
        "lastToolIntent": "",
        "pendingImages": [],
        "responseAt": None,
        "todo": "",
        "responseId": None,
    }

def project_tool_start(event, projection):
    data = event.get("data") or {}
    if isinstance(data.get("intent"), str):
        projection["lastToolIntent"] = data["intent"]
    call_id = data.get("toolCallId")
    if call_id:
        projection["pendingTools"][call_id] = data
    if data.get("toolName") == "ask":
        projection["activity"].append({
            "id": event.get("id"),
            "kind": "ask",
            "title": "OMP needs input",
            "detail": data.get("intent") or "Input required",
            "time": event_time(event),
        })

def project_tool_result(message, projection):
    call_id = message.get("toolCallId")
    if call_id:
        projection["pendingTools"].pop(call_id, None)
    projection["latestError"] = bool(message.get("isError"))
    for url in message_images(message):
        if url not in projection["pendingImages"]:
            projection["pendingImages"].append(url)
    if message.get("toolName") == "todo":
        projection["todo"] = message_text(message)
    if message.get("isError"):
        projection["activity"].append({
            "id": projection.get("currentEventId"),
            "kind": "error",
            "title": "Tool failed",
            "detail": message_text(message),
            "time": projection.get("currentEventTime"),
        })

def project_message(event, projection):
    message = event.get("message") or {}
    role = message.get("role")
    projection["latestMessage"] = message
    projection["latestError"] = False
    if role == "toolResult":
        project_tool_result(message, projection)
        return

    text = message_text(message)
    if role == "user" and text:
        projection["activity"].append({
            "id": event.get("id"),
            "kind": "user",
            "attachments": sum(
                1 for item in content_items(message)
                if item.get("type") in {"image", "file"}
            ),
            "title": "You",
            "detail": text,
            "time": event_time(event),
            "images": [{"url": url} for url in message_images(message)[:4]],
        })
    elif role == "assistant" and (
        message.get("errorMessage") or message.get("stopReason") == "error"
    ):
        projection["latestError"] = True
        projection["activity"].append({
            "id": event.get("id"),
            "kind": "error",
            "title": "OMP needs attention",
            "detail": message.get("errorMessage") or text,
            "time": event_time(event),
        })
    elif role == "assistant" and text:
        projection["lastAssistant"] = text
        projection["responseAt"] = event_time(event)
        projection["responseId"] = event.get("id")
        projection["activity"].append({
            "id": event.get("id"),
            "kind": "assistant",
            "title": "OMP",
            "detail": text,
            "time": event_time(event),
            "images": [{"url": url} for url in projection["pendingImages"][-4:]],
        })
        projection["pendingImages"].clear()

def project_event(event, projection):
    kind = event.get("type")
    if kind == "model_change":
        projection["model"] = event.get("model") or projection["model"]
    elif kind == "thinking_level_change":
        projection["thinking"] = event.get("thinkingLevel") or projection["thinking"]
    elif kind == "custom" and event.get("customType") == "tool_execution_start":
        project_tool_start(event, projection)
    elif kind == "message":
        projection["currentEventId"] = event.get("id")
        projection["currentEventTime"] = event_time(event)
        project_message(event, projection)

def serialize_session(header, projection, read_cursors, archive_activity=None, reset_at=None, event_ids=None):
    status, working_on = session_state(
        projection["pendingTools"],
        projection["latestError"],
        projection["latestMessage"],
        projection["lastToolIntent"],
        shorten,
    )
    execution = execution_state(status)
    try:
        modified = header["path"].stat().st_mtime
    except OSError:
        modified = time.time()
    activity = list(projection["activity"])
    all_activity = (archive_activity or []) + activity
    inbox_ids = [
        item["id"] for item in all_activity
        if item.get("id") and item.get("kind") == "assistant"
    ]
    cursor = read_cursors.get(header.get("sessionId"))
    cursor_index = event_ids.index(cursor) if cursor in (event_ids or []) else -1
    unread_ids = [
        event_id for event_id in inbox_ids
        if event_id in (event_ids or []) and (event_ids or []).index(event_id) > cursor_index
    ]
    summary = working_on or (activity[-1]["detail"] if activity else "")
    return {
        "sessionId": header.get("sessionId"),
        "model": projection["model"],
        "thinking": projection["thinking"],
        "status": status,
        "executionState": execution,
        "inboxState": "unread" if unread_ids else "read",
        "workingOn": working_on,
        "updatedAt": datetime.fromtimestamp(modified, timezone.utc).isoformat(),
        "updatedMs": int(modified * 1000),
        "preview": shorten(projection["lastAssistant"], 220),
        "activitySummary": shorten(summary, 120),
        "activity": activity,
        "archiveActivity": archive_activity or [],
        "contextResetAt": reset_at,
        "responseAt": projection["responseAt"],
        "responseId": inbox_ids[-1] if inbox_ids else None,
        "todo": projection["todo"],
        "eventIds": event_ids or [],
        "inboxEventIds": inbox_ids,
    }

def parse_session(header, read_cursors):
    if header["path"] is None:
        return blank_session(header)
    projection = new_session_projection(header)
    archive_activity = []
    reset_at = None
    event_ids = []
    for event in session_events(header["path"]):
        if event.get("id"):
            event_ids.append(event["id"])
        if event.get("type") == "reset_boundary":
            archive_activity.extend(projection["activity"])
            projection = new_session_projection(header)
            reset_at = event_time(event)
        else:
            project_event(event, projection)
    return serialize_session(header, projection, read_cursors, archive_activity, reset_at, event_ids)
