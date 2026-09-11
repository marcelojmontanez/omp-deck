from collections.abc import Callable, Mapping
from typing import Any, Protocol

from omp_rpc import UnknownNotification

from ._rpc_projection import objects


class SubagentProtocolError(ValueError):
    """A native subagent response does not match its published shape."""


class SubagentClient(Protocol):
    def request_raw(self, command_type: str, **payload: Any) -> dict[str, Any]: ...
    def on_unknown_notification(self, listener: Callable[[UnknownNotification], None]) -> Callable[[], None]: ...


def subscribe_subagents(client: SubagentClient, emit: Callable[[str, Mapping[str, Any]], None]) -> Callable[[], None]:
    def receive(notification: UnknownNotification) -> None:
        frame = notification.payload
        kind = frame.get("type")
        if kind not in {"subagent_lifecycle", "subagent_progress"}:
            return
        payload = frame.get("payload")
        if not isinstance(payload, Mapping):
            emit("protocol_error", {"error": "invalid subagent notification"})
            return
        emit(kind, payload)

    unsubscribe = client.on_unknown_notification(receive)
    try:
        result = client.request_raw("set_subagent_subscription", level="progress")
        if result.get("level") != "progress":
            raise SubagentProtocolError("subagent progress subscription was not accepted")
    except BaseException:
        unsubscribe()
        raise
    return unsubscribe


def snapshot_subagents(client: SubagentClient) -> tuple[dict[str, Any], ...]:
    response = client.request_raw("get_subagents")
    subagents = response.get("subagents")
    if not isinstance(subagents, list):
        raise SubagentProtocolError("invalid subagent snapshot")
    try:
        result = objects(subagents)
    except TypeError as exc:
        raise SubagentProtocolError("invalid subagent snapshot") from exc
    for item in result:
        progress = item.get("progress", {})
        if not isinstance(progress, Mapping):
            raise SubagentProtocolError("invalid subagent progress")
        item["label"] = item.get("description") or item.get("agent") or item.get("id")
        item["activitySummary"] = progress.get("lastIntent") or progress.get("currentTool") or item.get("status")
    return result
