from __future__ import annotations

import ipaddress
import json
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from omp_deck.auth_policy import auth_configuration
from omp_deck.settings import _read_config

from .worker_state import write_json

MAX_RESPONSE = 1024 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


def _fetch(url: str, cookie: str | None = None, *, host: str | None = None, login: str | None = None) -> dict:
    headers = {"Accept": "application/json"}
    if cookie:
        headers["Cookie"] = cookie
    if host:
        headers["Host"] = host
    if login:
        headers["Tailscale-User-Login"] = login
    request = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.build_opener(_NoRedirect).open(request, timeout=10) as response:
        if response.status != 200:
            raise RuntimeError("release verification endpoint was unhealthy")
        data = response.read(MAX_RESPONSE + 1)
    if len(data) > MAX_RESPONSE:
        raise RuntimeError("release verification response exceeded limit")
    value = json.loads(data)
    if not isinstance(value, dict):
        raise RuntimeError("release verification returned invalid JSON")
    return value


def verify_http(request: dict, credential: str, cookie_name: str) -> list[str]:
    profile = request["profile"]
    origin = request["origin"].rstrip("/")
    parsed = urlsplit(origin)
    try:
        loopback = ipaddress.ip_address(parsed.hostname or "").is_loopback
    except ValueError:
        loopback = (parsed.hostname or "").casefold() == "localhost"
    if (
        (parsed.scheme != "https" and not (parsed.scheme == "http" and loopback))
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError("release origin must be HTTPS or loopback HTTP")
    port = int(profile["port"])
    local = f"http://127.0.0.1:{port}"
    host = f"127.0.0.1:{port}"
    values = _read_config(Path(profile["config"]))
    mode, owner = auth_configuration(values, values.get("host", "127.0.0.1"))
    login = owner if mode == "tailscale" else None
    cookie = f"{cookie_name}={credential}"
    health = _fetch(local + "/healthz", host=host, login=login)
    if health.get("ok") is not True:
        raise RuntimeError("local health verification failed")
    config = _fetch(local + "/api/client-config", cookie, host=host, login=login)
    expected = request["candidate"]
    if config.get("build") != {"version": expected["wheelVersion"], "revision": expected["revision"]}:
        raise RuntimeError("installed build identity does not match candidate")
    authenticated_path = "/api/fleet" if request["kind"] == "coordinator" else "/api/sessions"
    _fetch(local + authenticated_path, cookie, host=host, login=login)
    verify_public(request, expected)
    return ["local-health", "local-identity", "local-auth"]


def verify_public(request: dict, expected: dict) -> None:
    origin = request["origin"].rstrip("/")
    if _fetch(origin + "/healthz").get("ok") is not True:
        raise RuntimeError("public health verification failed")
    config = _fetch(origin + "/api/client-config")
    if config.get("build") != {"version": expected["wheelVersion"], "revision": expected["revision"]}:
        raise RuntimeError("public build identity does not match the release")


def write_browser_input(request: dict, credential: str, cookie_name: str, path: Path) -> str:
    payload = {
        "version": 1,
        "transaction": request["transaction"],
        "url": request["origin"],
        "cookieName": cookie_name,
        "credential": credential,
        "kind": request["kind"],
        "machineIds": request.get("machine_ids", []),
        "expectedBuild": {
            "version": request["candidate"]["wheelVersion"],
            "revision": request["candidate"]["revision"],
        },
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    write_json(path, payload)
    return __import__("hashlib").sha256(encoded).hexdigest()
