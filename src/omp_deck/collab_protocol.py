"""Oh My Pi collaboration wire protocol (COLLAB_PROTO 3).

Secrets deliberately never appear in object representations or exception text.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import struct
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ._collab_validation import (
    GUEST_FRAME_TYPES as GUEST_FRAME_TYPES,
)
from ._collab_validation import (
    HOST_FRAME_TYPES as HOST_FRAME_TYPES,
)
from ._collab_validation import (
    validate_frame,
)
from ._collab_validation import (
    validate_guest_frame as validate_guest_frame,
)
from ._collab_validation import (
    validate_host_frame as validate_host_frame,
)

COLLAB_PROTO = 3
DEFAULT_RELAY_URL = "wss://my.omp.sh"
ROOM_ID_BYTES = 16
ROOM_KEY_BYTES = 32
WRITE_TOKEN_BYTES = 16
ENVELOPE_HEADER_LENGTH = 4
IV_LENGTH = 12
TAG_LENGTH = 16
# The relay's replicated plaintext limit is 1 MiB.  A packed binary message
# additionally carries the peer prefix and AES-GCM IV/authentication tag.
MAX_REPLICATED_PAYLOAD_BYTES = 1024 * 1024
MAX_FRAME_BYTES = MAX_REPLICATED_PAYLOAD_BYTES + ENVELOPE_HEADER_LENGTH + IV_LENGTH + TAG_LENGTH

_ROOM_RE = re.compile(r"[A-Za-z0-9_-]{10,64}\Z")
_B64_RE = re.compile(r"[A-Za-z0-9_-]+\Z")
_BARE_RE = re.compile(r"([A-Za-z0-9_-]{10,64})[.#]([A-Za-z0-9_-]+)\Z")
_PATH_RE = re.compile(r"/r/([A-Za-z0-9_-]{10,64})(?:\.([A-Za-z0-9_-]+))?\Z")
_LOCAL_HOSTS = frozenset(("localhost", "127.0.0.1", "::1"))


def b64url_encode(data: bytes) -> str:
    """Encode bytes as canonical, unpadded base64url."""
    if not isinstance(data, bytes):
        raise TypeError("data must be bytes")
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    """Decode canonical, unpadded base64url, rejecting padding and aliases."""
    if not isinstance(text, str) or not text or _B64_RE.fullmatch(text) is None:
        raise ValueError("invalid base64url value")
    try:
        decoded = base64.b64decode(text + "=" * (-len(text) % 4), altchars=b"-_", validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("invalid base64url value") from exc
    if b64url_encode(decoded) != text:
        raise ValueError("non-canonical base64url value")
    return decoded


@dataclass(frozen=True, slots=True, repr=False)
class ParsedCollabLink:
    ws_url: str
    room_id: str
    key: bytes
    write_token: bytes | None = None
    read_only: bool = field(init=False)

    def __post_init__(self) -> None:
        if len(self.key) != ROOM_KEY_BYTES:
            raise ValueError("room key must be 32 bytes")
        if self.write_token is not None and len(self.write_token) != WRITE_TOKEN_BYTES:
            raise ValueError("write token must be 16 bytes")
        object.__setattr__(self, "read_only", self.write_token is None)

    def __repr__(self) -> str:
        return (
            f"ParsedCollabLink(ws_url={self.ws_url!r}, room_id={self.room_id!r}, "
            f"key=<redacted>, write_token={'None' if self.write_token is None else '<redacted>'}, "
            f"read_only={self.read_only!r})"
        )


def _origin(parts: Any) -> str:
    if parts.username is not None or parts.password is not None:
        raise ValueError("collab link must not contain user information")
    try:
        hostname, port = parts.hostname, parts.port
    except ValueError as exc:
        raise ValueError("invalid collab link authority") from exc
    if not hostname:
        raise ValueError("collab link is missing a host")
    scheme = parts.scheme.lower()
    if scheme in ("wss", "https"):
        ws_scheme = "wss"
    elif scheme in ("ws", "http"):
        if hostname.lower() not in _LOCAL_HOSTS:
            raise ValueError("plain websocket links are only allowed for localhost")
        ws_scheme = "ws"
    else:
        raise ValueError("unsupported collab link scheme")
    rendered_host = f"[{hostname}]" if ":" in hostname else hostname
    return f"{ws_scheme}://{rendered_host}{':' + str(port) if port is not None else ''}"


def parse_collab_link(link: str) -> ParsedCollabLink:
    """Parse an official compact, full, custom-relay, or browser collab link."""
    if not isinstance(link, str):
        raise TypeError("collab link must be a string")
    text = link.strip()
    if not text or any(ch.isspace() for ch in text):
        raise ValueError("invalid collab link")
    # Preserve the one documented URL-stack compatibility transform only.
    text = re.sub(r"%23", "#", text, flags=re.IGNORECASE)
    bare = _BARE_RE.fullmatch(text)
    if bare:
        text = f"{DEFAULT_RELAY_URL}/r/{bare.group(1)}.{bare.group(2)}"
    elif "://" not in text:
        text = "wss://" + text
    try:
        parts = urlsplit(text)
    except ValueError as exc:
        raise ValueError("invalid collab link") from exc

    # A browser deep link has no relay room path of its own; its fragment is
    # exactly one complete collab link. A /r/...#secret URL is a legacy relay
    # link and is handled directly below.
    direct_match = _PATH_RE.fullmatch(parts.path)
    if parts.scheme.lower() in ("http", "https") and parts.fragment and direct_match is None:
        if parts.query:
            raise ValueError("ambiguous collab browser link")
        # Viewer routes are deployment-defined.  They may be nested, but must
        # remain an ordinary absolute path; the complete relay link is carried
        # only in the fragment.
        if (
            not parts.path.startswith("/")
            or "\\" in parts.path
            or any(segment in (".", "..") for segment in parts.path.split("/"))
        ):
            raise ValueError("unsafe collab browser link path")
        return parse_collab_link(parts.fragment)

    if parts.query:
        raise ValueError("collab link must not contain a query string")
    origin = _origin(parts)
    match = _PATH_RE.fullmatch(parts.path)
    if match is None:
        raise ValueError("collab link must contain one /r/<roomId> path")
    room_id, path_secret = match.groups()
    if not _ROOM_RE.fullmatch(room_id):
        raise ValueError("invalid room id")
    if path_secret is not None and parts.fragment:
        raise ValueError("collab link has ambiguous secrets")
    secret_text = path_secret or parts.fragment
    if not secret_text:
        raise ValueError("collab link is missing its secret")
    secret = b64url_decode(secret_text)
    if len(secret) not in (ROOM_KEY_BYTES, ROOM_KEY_BYTES + WRITE_TOKEN_BYTES):
        raise ValueError("collab secret must encode 32 or 48 bytes")
    return ParsedCollabLink(
        ws_url=f"{origin}/r/{room_id}",
        room_id=room_id,
        key=secret[:ROOM_KEY_BYTES],
        write_token=secret[ROOM_KEY_BYTES:] or None,
    )


def pack_envelope(peer_id: int, sealed: bytes, *, max_frame_bytes: int = MAX_FRAME_BYTES) -> bytes:
    if not isinstance(peer_id, int) or isinstance(peer_id, bool) or not 0 <= peer_id <= 0xFFFFFFFF:
        raise ValueError("peer id must be a uint32")
    if not isinstance(sealed, bytes):
        raise TypeError("sealed payload must be bytes")
    if len(sealed) + ENVELOPE_HEADER_LENGTH > max_frame_bytes:
        raise ValueError("envelope exceeds maximum frame size")
    return struct.pack(">I", peer_id) + sealed


def unpack_envelope(data: bytes, *, max_frame_bytes: int = MAX_FRAME_BYTES) -> tuple[int, bytes]:
    if not isinstance(data, bytes):
        raise TypeError("envelope must be bytes")
    if len(data) < ENVELOPE_HEADER_LENGTH:
        raise ValueError("envelope is too short")
    if len(data) > max_frame_bytes:
        raise ValueError("envelope exceeds maximum frame size")
    return struct.unpack(">I", data[:4])[0], data[4:]


def encode_frame(frame: Mapping[str, Any], *, max_frame_bytes: int = MAX_REPLICATED_PAYLOAD_BYTES) -> bytes:
    validated = validate_frame(frame)
    try:
        data = json.dumps(validated, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("collab frame is not valid JSON") from exc
    if len(data) > max_frame_bytes:
        raise ValueError("collab frame exceeds maximum size")
    return data


def decode_frame(
    data: bytes, *, max_frame_bytes: int = MAX_REPLICATED_PAYLOAD_BYTES, direction: str | None = None
) -> dict[str, Any]:
    if not isinstance(data, bytes):
        raise TypeError("frame payload must be bytes")
    if len(data) > max_frame_bytes:
        raise ValueError("collab frame exceeds maximum size")

    def strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name, value in pairs:
            if name in result:
                raise ValueError("duplicate JSON object key")
            result[name] = value
        return result

    def reject_constant(_value: str) -> Any:
        raise ValueError("invalid JSON number")

    try:
        frame = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=strict_object,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("invalid collab frame JSON") from exc
    return validate_frame(frame, direction=direction)


def seal_frame(key: bytes, frame: Mapping[str, Any], *, max_frame_bytes: int = MAX_REPLICATED_PAYLOAD_BYTES) -> bytes:
    if not isinstance(key, bytes) or len(key) != ROOM_KEY_BYTES:
        raise ValueError("room key must be 32 bytes")
    plaintext = encode_frame(frame, max_frame_bytes=max_frame_bytes)
    iv = os.urandom(IV_LENGTH)
    return iv + AESGCM(key).encrypt(iv, plaintext, None)


def open_frame(
    key: bytes, sealed: bytes, *, max_frame_bytes: int = MAX_REPLICATED_PAYLOAD_BYTES, direction: str | None = None
) -> dict[str, Any]:
    if not isinstance(key, bytes) or len(key) != ROOM_KEY_BYTES:
        raise ValueError("room key must be 32 bytes")
    if not isinstance(sealed, bytes):
        raise TypeError("sealed frame must be bytes")
    if len(sealed) < IV_LENGTH + TAG_LENGTH:
        raise ValueError("sealed frame is too short")
    if len(sealed) > IV_LENGTH + TAG_LENGTH + max_frame_bytes:
        raise ValueError("sealed frame exceeds maximum size")
    iv, ciphertext = sealed[:IV_LENGTH], sealed[IV_LENGTH:]
    plaintext = AESGCM(key).decrypt(iv, ciphertext, None)
    return decode_frame(plaintext, max_frame_bytes=max_frame_bytes, direction=direction)
