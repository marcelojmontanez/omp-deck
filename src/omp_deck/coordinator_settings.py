from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .auth_policy import auth_configuration
from .fleet_protocol import validate_origin
from .settings import _read_config
from .settings import allowed_hosts as build_allowed_hosts

_CONFIG_FIELDS = {
    "host",
    "port",
    "origin",
    "state_root",
    "allowed_hosts",
    "auth_mode",
    "tailscale_user",
}
COORDINATOR_COOKIE_NAME = "omp_deck_coordinator_device"
_HOSTNAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?\Z")


@dataclass(frozen=True)
class CoordinatorSettings:
    host: str
    port: int
    origin: str
    state_root: Path
    allowed_hosts: tuple[str, ...]
    auth_mode: str = "pairing"
    tailscale_user: str | None = None


def _configured_root(value, path: Path) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise RuntimeError(f"{path} must configure state_root")
    expanded = Path(value).expanduser()
    if not expanded.is_absolute():
        raise RuntimeError("state_root must be an absolute or home-relative path")
    return Path(os.path.abspath(expanded))


def _bind_host(value) -> tuple[str, bool]:
    if not isinstance(value, str) or not value or value.strip() != value or value.endswith(".") or "*" in value:
        raise RuntimeError("host must be a hostname or IP address")
    bracketed = value.startswith("[") or value.endswith("]")
    if bracketed and not (value.startswith("[") and value.endswith("]")):
        raise RuntimeError("host must be a hostname or IP address")
    candidate = value[1:-1] if bracketed else value
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        if bracketed or _HOSTNAME.fullmatch(candidate) is None or ".." in candidate:
            raise RuntimeError("host must be a hostname or IP address") from None
        return candidate.lower(), candidate.lower() == "localhost"
    return address.compressed, address.is_loopback


def _validate_allowed_hosts(values, host: str, origin: str) -> tuple[str, ...]:
    configured = values.get("allowed_hosts", [])
    if not isinstance(configured, list):
        raise RuntimeError("allowed_hosts must be a list of hostnames")
    normalized = []
    for item in configured:
        try:
            allowed_host, _ = _bind_host(item)
        except RuntimeError:
            raise RuntimeError("allowed_hosts must be a list of hostnames") from None
        normalized.append(allowed_host)
    origin_host = urlsplit(origin).hostname
    augmented = dict(values)
    augmented["allowed_hosts"] = normalized
    allowed = build_allowed_hosts(augmented, host)
    return tuple(dict.fromkeys((*allowed, origin_host)))


def load_coordinator_settings(path) -> CoordinatorSettings:
    if path is None:
        raise RuntimeError("coordinator commands require --config")
    config_path = Path(path).expanduser()
    values = _read_config(config_path)
    if not isinstance(values, dict):
        raise RuntimeError(f"{config_path} must contain a JSON object")
    unknown = set(values) - _CONFIG_FIELDS
    if unknown:
        raise RuntimeError(f"unsupported coordinator configuration field: {sorted(unknown)[0]}")
    if "origin" not in values:
        raise RuntimeError(f"{config_path} must configure origin")
    origin = validate_origin(values["origin"])
    state_root = _configured_root(values.get("state_root"), config_path)
    host, loopback = _bind_host(values.get("host", "127.0.0.1"))
    port = values.get("port", 8790)
    if type(port) is not int or not 1 <= port <= 65535:
        raise RuntimeError("port must be an integer between 1 and 65535")
    if not loopback and "allowed_hosts" not in values:
        raise RuntimeError("non-loopback host requires explicit allowed_hosts trusted exposure configuration")
    auth_mode, tailscale_user = auth_configuration(values, host)
    return CoordinatorSettings(
        host=host,
        port=port,
        origin=origin,
        state_root=state_root,
        allowed_hosts=_validate_allowed_hosts(values, host, origin),
        auth_mode=auth_mode,
        tailscale_user=tailscale_user,
    )
