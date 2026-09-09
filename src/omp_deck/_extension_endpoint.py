"""Shared endpoint identity for fallback extension Unix sockets."""

from __future__ import annotations

import hashlib


def unix_endpoint_name(session_id: str, connection_id: str) -> str:
    digest = hashlib.sha256()
    digest.update(session_id.encode("utf-8"))
    digest.update(b"\0")
    digest.update(connection_id.encode("utf-8"))
    return f"{digest.hexdigest()[:24]}.sock"
