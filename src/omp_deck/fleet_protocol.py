from __future__ import annotations

import base64
import binascii
import ipaddress
import json
import re
import time
import unicodedata
from typing import Any
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from .auth import AuthenticationFailed

TICKET_VERSION = 1
TICKET_LIFETIME_SECONDS = 60
MAX_TICKET_BYTES = 4096
MAX_MACHINE_NAME_LENGTH = 80

_HEX_ID = re.compile(r"[0-9a-f]{32}\Z")
_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_LEGACY_IP_LABEL = re.compile(r"0x[0-9a-f]+\Z")
_TICKET_KEYS = {"version", "issuer", "audience", "origin", "deviceId", "issuedAt", "expiresAt"}
_ENROLLMENT_KEYS = {
    "version",
    "machineId",
    "name",
    "coordinatorOrigin",
    "machineOrigin",
    "publicKey",
}


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str, *, size: int | None = None) -> bytes:
    if not isinstance(value, str) or not value or len(value) > MAX_TICKET_BYTES:
        raise ValueError("invalid base64url value")
    if re.fullmatch(r"[A-Za-z0-9_-]+", value) is None:
        raise ValueError("invalid base64url value")
    try:
        decoded = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (ValueError, binascii.Error) as error:
        raise ValueError("invalid base64url value") from error
    if _b64encode(decoded) != value or (size is not None and len(decoded) != size):
        raise ValueError("invalid base64url value")
    return decoded


def _canonical_host(host: str) -> str:
    if not host or host.endswith(".") or "*" in host or "%" in host or any(ord(character) > 127 for character in host):
        raise ValueError("invalid origin host")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        normalized = host.lower()
        labels = normalized.split(".")
        if (
            len(normalized) > 253
            or any(_DNS_LABEL.fullmatch(label) is None for label in labels)
            or labels[-1].isdigit()
            or any(_LEGACY_IP_LABEL.fullmatch(label) is not None for label in labels)
        ):
            raise ValueError("invalid origin host") from None
        return normalized
    return address.compressed


def validate_origin(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.strip() != value
        or "?" in value
        or "#" in value
        or any(ord(character) <= 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("invalid origin")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ValueError("invalid origin") from error
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.netloc.endswith(":")
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("invalid origin")
    host = _canonical_host(parsed.hostname or "")
    scheme = parsed.scheme.lower()
    if scheme == "http":
        try:
            local = ipaddress.ip_address(host).is_loopback
        except ValueError:
            local = host == "localhost"
        if not local:
            raise ValueError("HTTP origins must be loopback")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("invalid origin port")
    if ":" in host:
        host = f"[{host}]"
    default_port = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    suffix = "" if port is None or default_port else f":{port}"
    return f"{scheme}://{host}{suffix}"


def validate_machine_name(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.strip() != value
        or len(value) > MAX_MACHINE_NAME_LENGTH
        or any(unicodedata.category(character).startswith("C") for character in value)
    ):
        raise ValueError("invalid machine name")
    return value


def validate_machine_id(value: Any) -> str:
    if not isinstance(value, str) or _HEX_ID.fullmatch(value) is None:
        raise ValueError("invalid machine ID")
    return value


def validate_enrollment(value: dict) -> dict:
    if (
        not isinstance(value, dict)
        or set(value) != _ENROLLMENT_KEYS
        or type(value.get("version")) is not int
        or value["version"] != 1
    ):
        raise ValueError("invalid enrollment shape")
    public_key = value.get("publicKey")
    _b64decode(public_key, size=32)
    return {
        "version": 1,
        "machineId": validate_machine_id(value.get("machineId")),
        "name": validate_machine_name(value.get("name")),
        "coordinatorOrigin": validate_origin(value.get("coordinatorOrigin")),
        "machineOrigin": validate_origin(value.get("machineOrigin")),
        "publicKey": public_key,
    }


def _canonical_json(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def issue_ticket(
    private_key: str,
    *,
    issuer: str,
    machine_id: str,
    device_id: str,
    now: int | None = None,
) -> tuple[str, int]:
    issued_at = int(time.time()) if now is None else now
    if type(issued_at) is not int or issued_at < 0:
        raise ValueError("invalid issuance time")
    issuer = validate_origin(issuer)
    machine_id = validate_machine_id(machine_id)
    device_id = validate_machine_id(device_id)
    key = Ed25519PrivateKey.from_private_bytes(_b64decode(private_key, size=32))
    expires_at = issued_at + TICKET_LIFETIME_SECONDS
    claims = {
        "version": TICKET_VERSION,
        "issuer": issuer,
        "audience": machine_id,
        "origin": issuer,
        "deviceId": device_id,
        "issuedAt": issued_at,
        "expiresAt": expires_at,
    }
    payload = _canonical_json(claims)
    token = f"{_b64encode(payload)}.{_b64encode(key.sign(payload))}"
    if len(token.encode("ascii")) > MAX_TICKET_BYTES:
        raise ValueError("access ticket is too large")
    return token, expires_at


def verify_ticket(token: str, enrollment: dict, *, now: int | None = None) -> dict:
    current = int(time.time()) if now is None else now
    try:
        if type(current) is not int or current < 0 or not isinstance(token, str):
            raise ValueError("invalid ticket")
        if len(token.encode("utf-8")) > MAX_TICKET_BYTES or token.count(".") != 1:
            raise ValueError("invalid ticket")
        trusted = validate_enrollment(enrollment)
        encoded_payload, encoded_signature = token.split(".")
        payload = _b64decode(encoded_payload)
        signature = _b64decode(encoded_signature, size=64)
        claims = json.loads(payload)
        if not isinstance(claims, dict) or set(claims) != _TICKET_KEYS or _canonical_json(claims) != payload:
            raise ValueError("invalid ticket claims")
        if (
            type(claims["version"]) is not int
            or claims["version"] != TICKET_VERSION
            or not isinstance(claims["issuer"], str)
            or not isinstance(claims["audience"], str)
            or not isinstance(claims["origin"], str)
            or not isinstance(claims["deviceId"], str)
            or type(claims["issuedAt"]) is not int
            or type(claims["expiresAt"]) is not int
        ):
            raise ValueError("invalid ticket claims")
        issuer = validate_origin(claims["issuer"])
        origin = validate_origin(claims["origin"])
        validate_machine_id(claims["audience"])
        validate_machine_id(claims["deviceId"])
        issued_at = claims["issuedAt"]
        expires_at = claims["expiresAt"]
        if (
            issuer != claims["issuer"]
            or origin != claims["origin"]
            or issuer != trusted["coordinatorOrigin"]
            or origin != trusted["coordinatorOrigin"]
            or claims["audience"] != trusted["machineId"]
            or issued_at < 0
            or expires_at <= issued_at
            or expires_at - issued_at > TICKET_LIFETIME_SECONDS
            or issued_at > current + 5
            or current >= expires_at
        ):
            raise ValueError("invalid ticket claims")
        public_key = Ed25519PublicKey.from_public_bytes(_b64decode(trusted["publicKey"], size=32))
        public_key.verify(signature, payload)
        return claims
    except (InvalidSignature, UnicodeError, json.JSONDecodeError, RecursionError, TypeError, ValueError) as error:
        raise AuthenticationFailed("invalid or expired fleet access ticket") from error
