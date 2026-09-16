from __future__ import annotations

import base64
import binascii
import hmac
import ipaddress
import quopri
import re
from collections.abc import Mapping
from dataclasses import dataclass

from .auth import AuthenticationFailed

PAIRING_AUTH = "pairing"
TAILSCALE_AUTH = "tailscale"
AUTH_MODES = frozenset({PAIRING_AUTH, TAILSCALE_AUTH})
_MAX_LOGIN_BYTES = 1024
# Accommodates worst-case RFC2047 Q encoding of every accepted login byte.
_MAX_HEADER_BYTES = 4096
_ENCODED_LOGIN = re.compile(r"=\?utf-8\?([bq])\?([^?]+)\?=\Z", re.IGNORECASE)


def _valid_login(value: object) -> bool:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "," in value
        or any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        return False
    try:
        return len(value.encode("utf-8")) <= _MAX_LOGIN_BYTES
    except UnicodeEncodeError:
        return False


def _loopback_host(host: object) -> bool:
    if not isinstance(host, str) or not host:
        return False
    candidate = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    if candidate.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(candidate.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def auth_configuration(values: Mapping[str, object], host: object) -> tuple[str, str | None]:
    mode = values.get("auth_mode", PAIRING_AUTH)
    if not isinstance(mode, str) or mode not in AUTH_MODES:
        raise RuntimeError("auth_mode must be 'pairing' or 'tailscale'")
    owner_configured = "tailscale_user" in values
    owner = values.get("tailscale_user")
    if mode == PAIRING_AUTH:
        if owner_configured:
            raise RuntimeError("tailscale_user is only valid when auth_mode is 'tailscale'")
        return mode, None
    if not isinstance(owner, str) or not _valid_login(owner):
        raise RuntimeError("tailscale auth_mode requires one valid tailscale_user login")
    if not _loopback_host(host):
        raise RuntimeError("tailscale auth_mode requires a loopback listener")
    return mode, owner


def _decode_login_header(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise AuthenticationFailed("Tailscale identity is unavailable")
    try:
        oversized = len(value.encode("utf-8")) > _MAX_HEADER_BYTES
    except UnicodeEncodeError:
        oversized = True
    if oversized or any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise AuthenticationFailed("Tailscale identity is unavailable")
    if value.isascii() and not value.startswith("=?"):
        decoded = value
    else:
        words = value.split(" ")
        matches = [_ENCODED_LOGIN.fullmatch(word) for word in words]
        if not words or any(match is None for match in matches):
            raise AuthenticationFailed("Tailscale identity is unavailable")
        decoded_parts = []
        try:
            for match in matches:
                encoding, payload = match.groups()
                if encoding.casefold() == "b":
                    raw = base64.b64decode(payload, validate=True)
                else:
                    if re.search(r"=(?![0-9A-Fa-f]{2})", payload) is not None:
                        raise ValueError("malformed quoted-printable data")
                    raw = quopri.decodestring(payload.replace("_", " ").encode("ascii"))
                decoded_parts.append(raw.decode("utf-8"))
            decoded = "".join(decoded_parts)
        except (UnicodeError, ValueError, binascii.Error):
            raise AuthenticationFailed("Tailscale identity is unavailable") from None
    if not _valid_login(decoded):
        raise AuthenticationFailed("Tailscale identity is unavailable")
    return decoded


@dataclass(frozen=True)
class AuthPolicy:
    mode: str = PAIRING_AUTH
    tailscale_user: str | None = None

    @property
    def pairing_enabled(self) -> bool:
        return self.mode == PAIRING_AUTH

    def require_request_identity(self, headers, client_address) -> None:
        if self.mode == PAIRING_AUTH:
            return
        peer = client_address[0] if isinstance(client_address, tuple) and client_address else None
        if not _loopback_host(peer):
            raise AuthenticationFailed("Tailscale identity is unavailable")
        values = headers.get_all("Tailscale-User-Login", [])
        if len(values) != 1:
            raise AuthenticationFailed("Tailscale identity is unavailable")
        supplied = _decode_login_header(values[0])
        expected = self.tailscale_user or ""
        if not hmac.compare_digest(
            supplied.encode("utf-8"),
            expected.encode("utf-8"),
        ):
            raise AuthenticationFailed("Tailscale identity is unavailable")
