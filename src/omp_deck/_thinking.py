from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
THINKING_LEVEL_SET = frozenset(THINKING_LEVELS)
REQUEST_THINKING_LEVELS = THINKING_LEVEL_SET | {"auto"}


def normalized_thinking(value: Any) -> str | None:
    return value if isinstance(value, str) and value in THINKING_LEVEL_SET else None


def thinking_levels_for_model(model: Any) -> tuple[str, ...]:
    if not isinstance(model, Mapping) or model.get("reasoning") is not True:
        return ()
    thinking = model.get("thinking")
    if not isinstance(thinking, Mapping):
        return ()
    efforts = thinking.get("efforts")
    if not isinstance(efforts, Sequence) or isinstance(efforts, (str, bytes, bytearray)):
        return ()
    supported = {level for level in efforts if isinstance(level, str) and level in THINKING_LEVEL_SET}
    return ("off", *(level for level in THINKING_LEVELS[1:] if level in supported))


def validated_thinking_levels(value: Any) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError("thinking levels must be an array")
    levels = tuple(value)
    if (
        any(not isinstance(level, str) or level not in THINKING_LEVEL_SET for level in levels)
        or len(set(levels)) != len(levels)
        or levels != tuple(level for level in THINKING_LEVELS if level in levels)
    ):
        raise ValueError("invalid thinking levels")
    return levels
