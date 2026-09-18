from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from ._attachment_limits import platform_attachment_limits
from ._platform_capabilities import platform_capabilities


def registry_session(record: Mapping[str, Any], existing: Mapping[str, Any] | None) -> dict[str, Any]:
    """Project a durable registry record without discarding newer live or presentation state."""
    project = record.get("project")
    if not isinstance(project, Mapping):
        raise ValueError("registry project must be an object")
    adapter_kind = str(record["adapterKind"])
    live = existing is not None and existing["transport"]["instanceId"] is not None
    session = {
        "id": str(record["id"]),
        "ompSessionId": record.get("ompSessionId"),
        "title": str(record["title"]),
        "project": {"name": str(project["name"]), "path": str(project["path"])},
        "state": "disconnected",
        "transport": _disconnected_transport(adapter_kind),
        "historyError": None,
        "capabilities": platform_capabilities(adapter_kind),
        "attachmentLimits": platform_attachment_limits(adapter_kind),
        "messages": [],
        "todos": [],
        "subagents": [],
        "pendingUiRequest": None,
        "unread": False,
        "updatedAt": str(record["updatedAt"]),
        "workingOn": None,
        "model": None,
        "thinking": None,
        "thinkingLevels": [],
        "_desiredRpcRunning": bool(record.get("desiredRpcRunning", False)),
        "_generation": int(record["generation"]),
        "_adapterSequence": 0,
        "_resyncRequired": False,
        "_resyncWatermark": 0,
        "_activeTools": {},
        "_toolActivityOverflow": False,
        "_extensionThinkingControl": False,
    }
    if existing is not None:
        session.update(_retained_session_state(existing))
    if live:
        session.update(_retained_live_state(existing))
    session.update(_presentation_state(record, existing))
    return session


def _disconnected_transport(adapter_kind: str) -> dict[str, Any]:
    return {
        "kind": adapter_kind,
        "status": "disconnected",
        "label": "Managed RPC" if adapter_kind == "rpc" else adapter_kind.title(),
        "instanceId": None,
        "reason": "not connected",
    }


def _retained_session_state(existing: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "state": existing.get("state", "disconnected"),
        "transport": copy.deepcopy(existing["transport"]),
        "messages": copy.deepcopy(existing.get("messages", [])),
        "attachmentLimits": copy.deepcopy(existing.get("attachmentLimits")),
        "todos": copy.deepcopy(existing.get("todos", [])),
        "subagents": copy.deepcopy(existing.get("subagents", [])),
        "pendingUiRequest": copy.deepcopy(existing.get("pendingUiRequest")),
        "unread": bool(existing.get("unread", False)),
        "workingOn": existing.get("workingOn"),
        "model": copy.deepcopy(existing.get("model")),
        "thinking": existing.get("thinking"),
        "thinkingLevels": copy.deepcopy(existing.get("thinkingLevels", [])),
    }


def _retained_live_state(existing: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "ompSessionId": existing.get("ompSessionId"),
        "title": existing.get("title"),
        "historyError": existing.get("historyError"),
        "_adapterSequence": int(existing.get("_adapterSequence", 0)),
        "_resyncRequired": bool(existing.get("_resyncRequired", False)),
        "_resyncWatermark": int(existing.get("_resyncWatermark", 0)),
        "_activeTools": copy.deepcopy(existing.get("_activeTools", {})),
        "_toolActivityOverflow": bool(existing.get("_toolActivityOverflow", False)),
        "_extensionThinkingControl": bool(existing.get("_extensionThinkingControl", False)),
    }


def _presentation_state(
    record: Mapping[str, Any],
    existing: Mapping[str, Any] | None,
) -> dict[str, Any]:
    record_revision = int(record.get("presentationRevision", 0))
    existing_revision = int(existing.get("_presentationRevision", 0)) if existing is not None else -1
    if existing is not None and record_revision <= existing_revision:
        return {
            "displayName": existing.get("displayName"),
            "pinned": bool(existing.get("pinned", False)),
            "_presentationRevision": existing_revision,
        }
    return {
        "displayName": record.get("displayName"),
        "pinned": bool(record.get("pinned", False)),
        "_presentationRevision": record_revision,
    }
