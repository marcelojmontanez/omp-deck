from __future__ import annotations

from typing import Any

from .attachments import (
    COLLAB_MAX_IMAGE_BYTES,
    LEGACY_EXTENSION_MAX_IMAGE_BYTES,
    MAX_IMAGE_BYTES,
    MAX_IMAGES,
)


def platform_attachment_limits(adapter_kind: str) -> dict[str, int] | None:
    if adapter_kind == "rpc":
        max_bytes = MAX_IMAGE_BYTES
    elif adapter_kind == "collab":
        max_bytes = COLLAB_MAX_IMAGE_BYTES
    else:
        return None
    return {"maxImages": MAX_IMAGES, "maxTotalBytes": max_bytes}


def extension_attachment_limits(registration: Any, available: bool) -> dict[str, int | str] | None:
    if not available:
        return None
    limits: dict[str, int | str] = dict(
        registration.attachment_limits or {"maxImages": MAX_IMAGES, "maxTotalBytes": LEGACY_EXTENSION_MAX_IMAGE_BYTES}
    )
    if not getattr(registration, "attachment_limits_advertised", False):
        limits["reason"] = (
            "When this OMP session is idle, restart OMP and resume the session to activate "
            "the updated Deck extension and its 20 MiB image limit."
        )
    return limits
