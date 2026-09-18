from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit

from .storage import ReleaseError, absolute, identifier, load

HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@:-]{0,200}\Z")
PROFILE_PATHS = (
    "home",
    "uv",
    "base_python",
    "tool_root",
    "config",
    "unit",
    "state_root",
    "rollback_root",
)


def configuration(path: Path) -> dict:
    value = load(path)
    absolute(value.get("state_root"))
    targets = value.get("targets")
    if not isinstance(targets, list) or not 1 <= len(targets) <= 16:
        raise ReleaseError("configuration needs between one and sixteen targets")
    seen = set()
    for target in targets:
        target_config(target)
        if target["id"] in seen:
            raise ReleaseError("duplicate target identifier")
        seen.add(target["id"])
    if not any(item["kind"] == "node" for item in targets):
        raise ReleaseError("a fleet release needs a node as its first deployment")
    return value


def target_config(target: dict) -> None:
    if not isinstance(target, dict):
        raise ReleaseError("invalid target configuration")
    identifier(target.get("id"))
    if target.get("kind") not in {"node", "coordinator"}:
        raise ReleaseError("unsupported target kind")
    host = target.get("ssh")
    if host is not None and (not isinstance(host, str) or not HOST.fullmatch(host)):
        raise ReleaseError("invalid SSH destination")
    absolute(target.get("work_root"))
    absolute(target.get("bootstrap_rollback"))
    profile = target.get("profile")
    if not isinstance(profile, dict) or profile.get("platform") not in {"darwin", "linux"}:
        raise ReleaseError("automatic rollout currently supports macOS and Linux targets")
    for key in PROFILE_PATHS:
        absolute(profile.get(key))
    if target["kind"] == "node":
        for key in ("omp", "entrypoint", "plugin_expected"):
            absolute(profile.get(key))
    else:
        absolute(profile.get("releases_root"))
    if (
        isinstance(profile.get("port"), bool)
        or not isinstance(profile.get("port"), int)
        or not 1 <= profile["port"] <= 65535
    ):
        raise ReleaseError("invalid service port")
    parsed = urlsplit(target.get("origin", ""))
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise ReleaseError("target origin cannot contain credentials, paths, or parameters")
    if not parsed.hostname or not (
        parsed.scheme == "https" or parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    ):
        raise ReleaseError("target origin requires HTTPS or isolated loopback HTTP")
    machines = target.get("machine_ids", [])
    if not isinstance(machines, list) or len(machines) > 64 or any(not isinstance(item, str) for item in machines):
        raise ReleaseError("invalid expected machine identities")
    if target["kind"] == "coordinator" and not machines:
        raise ReleaseError("coordinator verification requires expected machine identities")


def deployment_order(targets: list[dict]) -> list[dict]:
    # A verified node is the canary. Keep coordinators behind all node checks.
    return [item for item in targets if item["kind"] == "node"] + [
        item for item in targets if item["kind"] == "coordinator"
    ]
