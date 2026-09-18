from collections.abc import Mapping
from typing import Any

from ._fleet_projection import has_visible_content


def latest_assistant_id(session: Mapping[str, Any]) -> str | None:
    for message in reversed(session["messages"]):
        if message.get("role") == "assistant" and isinstance(message.get("id"), str) and has_visible_content(message):
            return message["id"]
    return None


def project_unread(session: dict[str, Any], cursor: str | None) -> None:
    latest = latest_assistant_id(session)
    if latest is None or latest == cursor:
        session["unread"] = False
        return
    passed_latest = False
    for message in reversed(session["messages"]):
        if message.get("id") == cursor:
            session["unread"] = passed_latest
            return
        if message.get("id") == latest:
            passed_latest = True
    session["unread"] = True
