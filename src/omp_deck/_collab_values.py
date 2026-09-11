"""Strict validators for values nested in published Collab frames."""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any


def _fail(message: str = "malformed collab frame") -> None:
    raise ValueError(message)


def _obj(value: Any, required: set[str], optional: set[str] | None = None) -> dict[str, Any]:
    optional = optional or set()
    if not isinstance(value, dict) or set(value) - required - optional or not required <= set(value):
        _fail()
    return value


def _str(value: Any) -> None:
    if not isinstance(value, str):
        _fail()


def _bool(value: Any) -> None:
    if not isinstance(value, bool):
        _fail()


def _num(value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        _fail()


def _uint(value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _fail()


def _literal(value: Any, choices: set[str]) -> None:
    if not isinstance(value, str) or value not in choices:
        _fail()


def _array(value: Any, check: Callable[[Any], None]) -> None:
    if not isinstance(value, list):
        _fail()
    for item in value:
        check(item)


def _json(value: Any) -> None:
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        _num(value)
        return
    if isinstance(value, list):
        _array(value, _json)
        return
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        for item in value.values():
            _json(item)
        return
    _fail()


def _text_content(value: dict[str, Any]) -> None:
    item = _obj(value, {"type", "text"}, {"textSignature"})
    _str(item["text"])
    if "textSignature" in item:
        _str(item["textSignature"])


def _image_content(value: dict[str, Any]) -> None:
    item = _obj(value, {"type", "data", "mimeType"}, {"detail", "providerFile", "url"})
    _str(item["data"])
    _str(item["mimeType"])
    if "detail" in item:
        _literal(item["detail"], {"auto", "low", "high", "original"})
    if "providerFile" in item:
        _json(item["providerFile"])
    if "url" in item:
        _str(item["url"])


def _thinking_content(value: dict[str, Any]) -> None:
    item = _obj(value, {"type", "thinking"}, {"thinkingSignature", "itemId"})
    _str(item["thinking"])
    for key in ("thinkingSignature", "itemId"):
        if key in item:
            _str(item[key])


def _redacted_thinking_content(value: dict[str, Any]) -> None:
    item = _obj(value, {"type", "data"})
    _str(item["data"])


def _tool_call_content(value: dict[str, Any]) -> None:
    item = _obj(
        value,
        {"type", "id", "name", "arguments"},
        {"intent", "thoughtSignature", "rawBlock", "customWireName", "providerMetadata"},
    )
    _str(item["id"])
    _str(item["name"])
    arguments = item["arguments"]
    _obj(arguments, set(), set(arguments) if isinstance(arguments, dict) else set())
    _json(arguments)
    for key in ("intent", "thoughtSignature", "rawBlock", "customWireName"):
        if key in item:
            _str(item[key])
    if "providerMetadata" in item:
        _json(item["providerMetadata"])


def _fallback_content(value: dict[str, Any]) -> None:
    item = _obj(value, {"type", "from", "to"})
    for key in ("from", "to"):
        endpoint = _obj(item[key], {"model"})
        _str(endpoint["model"])


def _anthropic_server_tool_content(value: dict[str, Any]) -> None:
    item = _obj(value, {"type", "block"})
    block = item["block"]
    if not isinstance(block, dict) or not isinstance(block.get("type"), str):
        _fail()
    _json(block)


_SHARED_CONTENT_VALIDATORS: dict[str, Callable[[dict[str, Any]], None]] = {
    "text": _text_content,
    "image": _image_content,
}
_ASSISTANT_CONTENT_VALIDATORS: dict[str, Callable[[dict[str, Any]], None]] = {
    "thinking": _thinking_content,
    "redactedThinking": _redacted_thinking_content,
    "toolCall": _tool_call_content,
    "fallback": _fallback_content,
    "anthropicServerTool": _anthropic_server_tool_content,
}


def _content(value: Any, *, assistant: bool = False) -> None:
    if not isinstance(value, dict) or not isinstance(value.get("type"), str):
        _fail()
    validator = _SHARED_CONTENT_VALIDATORS.get(value["type"])
    if validator is None and assistant:
        validator = _ASSISTANT_CONTENT_VALIDATORS.get(value["type"])
    if validator is None:
        _fail()
    validator(value)


def _message(value: Any) -> None:
    if not isinstance(value, dict):
        _fail()
    role = value.get("role")
    if role in ("user", "developer"):
        optional = {"synthetic", "steering", "historyRewriteAt", "attribution", "providerPayload", "userInitiated"}
        item = _obj(value, {"role", "content", "timestamp"}, optional)
        content = item["content"]
        if not isinstance(content, str):
            _array(content, _content)
        for key in ("synthetic", "steering", "userInitiated"):
            if key in item:
                _bool(item[key])
        if "historyRewriteAt" in item:
            _num(item["historyRewriteAt"])
        for key in ("attribution", "providerPayload"):
            if key in item:
                _json(item[key])
        _num(item["timestamp"])
    elif role == "assistant":
        optional = {
            "api",
            "provider",
            "contextSnapshot",
            "retryRecovery",
            "responseId",
            "upstreamProvider",
            "upstreamModel",
            "stopDetails",
            "errorMessage",
            "errorClassificationMessage",
            "toolCallAbortMessages",
            "errorStatus",
            "errorId",
            "disabledFeatures",
            "inputTransformations",
            "providerPayload",
            "duration",
            "ttft",
            "completedAt",
        }
        item = _obj(value, {"role", "content", "model", "usage", "stopReason", "timestamp"}, optional)
        _array(item["content"], lambda x: _content(x, assistant=True))
        _str(item["model"])
        usage = _obj(
            item["usage"],
            {"input", "output", "cacheRead", "cacheWrite", "totalTokens", "cost"},
            {"premiumRequests", "orchestration", "cttl"},
        )
        for key in ("input", "output", "cacheRead", "cacheWrite", "totalTokens"):
            _num(usage[key])
        cost = _obj(usage["cost"], {"total"}, {"input", "output", "cacheRead", "cacheWrite", "orchestration", "cttl"})
        for amount in cost.values():
            _num(amount)
        for key in set(usage) - {"cost"}:
            _json(usage[key])
        _literal(item["stopReason"], {"stop", "length", "toolUse", "error", "aborted"})
        _num(item["timestamp"])
        for key in (
            "api",
            "provider",
            "responseId",
            "upstreamProvider",
            "upstreamModel",
            "errorMessage",
            "errorClassificationMessage",
        ):
            if key in item:
                _str(item[key])
        for key in ("errorStatus", "errorId", "duration", "ttft", "completedAt"):
            if key in item:
                _num(item[key])
        for key in (
            "contextSnapshot",
            "retryRecovery",
            "stopDetails",
            "toolCallAbortMessages",
            "disabledFeatures",
            "inputTransformations",
            "providerPayload",
        ):
            if key in item:
                _json(item[key])
    elif role == "toolResult":
        optional = {"details", "attribution", "prunedAt", "providerMetadata", "providerPayload", "useless"}
        item = _obj(value, {"role", "toolCallId", "toolName", "content", "isError", "timestamp"}, optional)
        _str(item["toolCallId"])
        _str(item["toolName"])
        _array(item["content"], _content)
        _bool(item["isError"])
        _num(item["timestamp"])
        if "prunedAt" in item:
            _num(item["prunedAt"])
        if "useless" in item:
            _bool(item["useless"])
        for key in ("details", "attribution", "providerMetadata", "providerPayload"):
            if key in item:
                _json(item[key])
    else:
        _fail()
