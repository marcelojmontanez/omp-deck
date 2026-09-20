from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import time
from pathlib import Path
from typing import Any

from omp_deck._linux_service import systemd_quote


def prepare(request: dict[str, Any], profile: dict[str, Any], root: Path, manifest: Path) -> dict[str, Any]:
    from . import coordinator as c

    if manifest.exists():
        state = c._load(manifest)
        digest = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
        if state["requestDigest"] != digest:
            raise c.CoordinatorError("transaction request changed")
        installed = {"version": state["rollback"]["wheelVersion"], "revision": state["rollback"]["revision"]}
        return c._result(state["decision"], installed, state.get("deadline"))

    candidate, wheel = c._wheel(request.get("candidate"))
    rollback, _ = c._wheel(request.get("rollback"))
    if candidate["wheelSha256"] == rollback["wheelSha256"]:
        raise c.CoordinatorError("candidate and rollback are identical")

    unit = c._read(profile["unit"])
    commands = c._unit_commands(unit)
    old = [
        str(profile["tool_root"] / "bin/python"),
        "-m",
        "omp_deck",
        "coordinator",
        "serve",
        "--config",
        str(profile["config"]),
    ]
    canonical = ("ExecStart=" + " ".join(systemd_quote(value) for value in old)).encode()
    actual_line = commands[0][1].rstrip(b"\r\n") if len(commands) == 1 else b""
    if len(commands) != 1 or commands[0][2] != old or actual_line != canonical:
        raise c.CoordinatorError("unexpected coordinator unit command")
    if not c._active(profile):
        raise c.CoordinatorError("coordinator service is not running")
    c._health(profile)
    current = c._live_identity(profile)
    expected_rollback = {"version": rollback["wheelVersion"], "revision": rollback["revision"]}
    if current != expected_rollback:
        raise c.CoordinatorError("installed coordinator identity mismatch")

    release = profile["releases_root"] / request["transaction"]
    release.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent_info = release.parent.stat(follow_symlinks=False)
    if (
        release.parent.is_symlink()
        or not stat.S_ISDIR(parent_info.st_mode)
        or parent_info.st_uid != os.geteuid()
        or stat.S_IMODE(parent_info.st_mode) != 0o700
    ):
        raise c.CoordinatorError("unsafe coordinator releases root")
    if release.exists() or release.is_symlink():
        raise c.CoordinatorError("immutable release already exists")

    frozen = c._run([profile["uv"], "pip", "freeze", "--python", profile["tool_root"] / "bin/python"], profile, 60)
    constraints = (
        b"\n".join(line for line in frozen.splitlines() if not line.startswith((b"omp-deck==", b"omp-deck @ "))) + b"\n"
    )
    release.mkdir(mode=0o700)
    try:
        c._run([profile["uv"], "venv", "--python", profile["base_python"], release], profile)
        c._atomic(release / "constraints.txt", constraints)
        c._run(
            [
                profile["uv"],
                "pip",
                "install",
                "--offline",
                "--python",
                release / "bin/python",
                "--constraints",
                release / "constraints.txt",
                wheel,
            ],
            profile,
        )
        expected_candidate = {"version": candidate["wheelVersion"], "revision": candidate["revision"]}
        if c._identity(release, profile) != expected_candidate:
            raise c.CoordinatorError("staged coordinator identity mismatch")
        ending = b"\r\n" if commands[0][1].endswith(b"\r\n") else b"\n" if commands[0][1].endswith(b"\n") else b""
        proposed_line = (
            "ExecStart=" + " ".join(systemd_quote(value) for value in [str(release / "bin/python"), *old[1:]])
        ).encode() + ending
        lines = unit.splitlines(keepends=True)
        lines[commands[0][0]] = proposed_line
        proposed = b"".join(lines)
        c._atomic(root / "guard/coordinator-unit.backup", unit)
        c._atomic(root / "guard/coordinator-unit.proposed", proposed)
        state = {
            "version": c.SCHEMA,
            "transaction": request["transaction"],
            "decision": "prepared",
            "preparedAt": time.time(),
            "deadline": None,
            "releaseRoot": str(release),
            "candidate": candidate,
            "rollback": rollback,
            "unitMode": stat.S_IMODE(profile["unit"].stat().st_mode),
            "oldUnitSha256": hashlib.sha256(unit).hexdigest(),
            "newUnitSha256": hashlib.sha256(proposed).hexdigest(),
            "configSha256": c._hash(profile["config"], private=True),
            "fleetSha256": c._hash(profile["state_root"] / "fleet-coordinator.json", private=True),
            "serviceEnabled": c._enabled(profile),
            "requestDigest": hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest(),
        }
        c._save(manifest, state)
    except BaseException:
        shutil.rmtree(release, ignore_errors=True)
        raise
    return c._result("prepared", expected_rollback)
