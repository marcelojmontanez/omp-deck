from __future__ import annotations

import time
from pathlib import Path
from typing import Any


def rollback(profile: dict[str, Any], state: dict[str, Any], manifest: Path) -> dict[str, Any]:
    from . import coordinator as c

    expected = {"version": state["rollback"]["wheelVersion"], "revision": state["rollback"]["revision"]}
    if state["decision"] == "rolledback":
        return c._result("rolledback", expected)
    if state["decision"] == "verified":
        raise c.CoordinatorError("verified coordinator release cannot be rolled back by its expired guard")
    try:
        c._guard(state, profile)
        current = c._hash(profile["unit"])
        if current not in {state["oldUnitSha256"], state["newUnitSha256"]}:
            raise c.CoordinatorError("coordinator unit has unrelated changes")
        backup = manifest.parent / "coordinator-unit.backup"
        if c._hash(backup, private=True) != state["oldUnitSha256"]:
            raise c.CoordinatorError("coordinator unit backup changed")
        if c._identity(profile["tool_root"], profile) != expected:
            raise c.CoordinatorError("retained rollback coordinator identity mismatch")
        auth_path = profile["state_root"] / "auth.json"
        if "rollbackAuthSha256" not in state:
            state["rollbackAuthSha256"] = c._hash(auth_path, private=True)
            state["rollbackPhase"] = "guarded"
            c._save(manifest, state)
        if c._hash(auth_path, private=True) != state["rollbackAuthSha256"]:
            raise c.CoordinatorError("coordinator authentication changed during rollback")
        if current != state["oldUnitSha256"]:
            state["rollbackPhase"] = "restoring-unit"
            c._save(manifest, state)
            c._atomic(profile["unit"], c._read(backup, private=True), state["unitMode"])
        # Disk bytes and the live process cannot prove systemd's cached unit.
        # Reconcile the definition even after an interrupted prior restoration.
        state["rollbackPhase"] = "reloading-unit"
        c._save(manifest, state)
        c._systemctl(profile, "daemon-reload")
        state["rollbackPhase"] = "unit-reloaded"
        c._save(manifest, state)

        running_identity = None
        if c._active(profile):
            try:
                c._health(profile)
                running_identity = c._live_identity(profile)
            except c.CoordinatorError:
                pass
        if running_identity != expected:
            state["rollbackPhase"] = "restarting-service"
            c._save(manifest, state)
            c._systemctl(profile, "restart", profile["service_name"])
        if not c._active(profile):
            raise c.CoordinatorError("rolled back coordinator did not start")
        c._health(profile)
        installed = c._live_identity(profile)
        if installed != expected:
            raise c.CoordinatorError("running rollback coordinator identity mismatch")
        if c._hash(auth_path, private=True) != state["rollbackAuthSha256"]:
            raise c.CoordinatorError("coordinator authentication changed during rollback")
        c._guard(state, profile)
        state["decision"] = "rolledback"
        state["deadline"] = None
        state["rollbackPhase"] = "complete"
        state["rolledBackAt"] = time.time()
        c._save(manifest, state)
        return c._result("rolledback", installed)
    except BaseException:
        state["decision"] = "rollback_failed"
        state["deadline"] = None
        c._save(manifest, state)
        raise
