import dataclasses
from collections.abc import Mapping
from enum import Enum
from typing import Any

from .rpc_adapter import SessionAdapter


def plain(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return {
            field.name: plain(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [plain(item) for item in value]
    return value


def invoke(
    adapter: SessionAdapter,
    action: str,
    payload: Mapping[str, Any],
) -> Any:
    if action == "sendMessage":
        adapter.send_message(
            payload["message"],
            images=payload.get("images"),
            streaming_behavior=payload.get("streamingBehavior"),
        )
        return None
    if action == "followUp":
        adapter.follow_up(payload["message"], images=payload.get("images"))
        return None
    if action == "interrupt":
        adapter.interrupt()
        return None
    if action == "setModel":
        return adapter.set_model(payload["provider"], payload["modelId"])
    if action == "setThinking":
        adapter.set_thinking(payload["level"])
        return None
    if action == "branch":
        return adapter.branch(payload["entryId"])
    if action == "switchSession":
        return adapter.switch(payload["sessionPath"])
    if action == "compact":
        return adapter.compact(payload.get("customInstructions"))
    if action == "respondToUi":
        return adapter.respond_ui(payload["requestId"], payload["value"])
    if action == "cancelUi":
        return adapter.cancel_ui(payload["requestId"])
    raise ValueError(f"unsupported RPC action: {action}")
