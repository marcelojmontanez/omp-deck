from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol


class UiRequestLike(Protocol):
    id: str
    method: str
    title: str | None
    message: str | None
    options: Sequence[str] | None
    option_details: Sequence[Mapping[str, object]] | None
    prefill: str | None
    placeholder: str | None
    timeout: int | None


@dataclass(frozen=True, slots=True)
class UiOption:
    value: str
    label: str
    description: str | None = None


@dataclass(frozen=True, slots=True)
class PendingUiRequest:
    id: str
    kind: str
    title: str | None
    message: str | None
    options: tuple[UiOption, ...]
    allow_cancel: bool
    allow_custom: bool
    prefill: str | None
    custom_placeholder: str | None
    expires_at: float | None


def plain(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return {field.name: plain(getattr(value, field.name)) for field in dataclasses.fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [plain(item) for item in value]
    return value


def objects(values: Sequence[Any]) -> tuple[dict[str, Any], ...]:
    result: list[dict[str, Any]] = []
    for value in values:
        projected = plain(value)
        if not isinstance(projected, dict):
            raise TypeError("RPC snapshot item is not an object")
        result.append(projected)
    return tuple(result)


def project_ui_request(request: UiRequestLike, now: float) -> PendingUiRequest:
    details = request.option_details or ()
    options = tuple(
        _project_option(value, details[index] if index < len(details) else {})
        for index, value in enumerate(request.options or ())
    )
    deadline = now + request.timeout / 1000 if request.timeout is not None else None
    return PendingUiRequest(
        id=request.id,
        kind=request.method,
        title=request.title,
        message=request.message,
        options=options,
        allow_cancel=True,
        allow_custom=request.method in {"input", "editor"},
        prefill=request.prefill,
        custom_placeholder=request.placeholder,
        expires_at=deadline,
    )


def _project_option(value: str, detail: Mapping[str, object]) -> UiOption:
    description = detail.get("description")
    return UiOption(
        value=value,
        label=str(detail.get("label", value)),
        description=description if isinstance(description, str) else None,
    )
