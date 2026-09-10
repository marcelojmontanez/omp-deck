from __future__ import annotations

import json
import os
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ._private_state import PrivateStateFile
from .fleet_protocol import (
    _b64decode,
    _b64encode,
    validate_enrollment,
    validate_machine_id,
    validate_machine_name,
    validate_origin,
)

STATE_VERSION = 1
MAX_MACHINES = 32
MAX_STATE_BYTES = 64 * 1024
_STATE_KEYS = {"version", "privateKey", "machines"}
_MACHINE_KEYS = {"id", "name", "origin"}


class CoordinatorStateError(RuntimeError):
    pass


class CoordinatorStore:
    def __init__(self, state_root: str | os.PathLike[str], coordinator_origin: str):
        self.state_root = Path(state_root)
        self._coordinator_origin = validate_origin(coordinator_origin)
        self.path = self.state_root / "fleet-coordinator.json"
        self._storage = PrivateStateFile(
            self.path,
            lock_name=".fleet-coordinator.lock",
            repair_root=os.name == "nt",
        )
        self._lock = threading.RLock()
        with self._locked_state(create=True):
            pass

    @property
    def private_key(self) -> str:
        with self._locked_state():
            return self._state["privateKey"]

    @property
    def public_key(self) -> str:
        with self._locked_state():
            key = Ed25519PrivateKey.from_private_bytes(_b64decode(self._state["privateKey"], size=32))
            return _b64encode(key.public_key().public_bytes_raw())

    def machines(self) -> tuple[dict, ...]:
        with self._locked_state():
            return tuple(dict(machine) for machine in self._state["machines"])

    def access_material(self) -> tuple[str, tuple[dict, ...]]:
        with self._locked_state():
            return self._state["privateKey"], tuple(dict(machine) for machine in self._state["machines"])

    def add_machine(self, name: str, origin: str) -> dict:
        name = validate_machine_name(name)
        origin = validate_origin(origin)
        with self._locked_state():
            machines = self._state["machines"]
            if len(machines) >= MAX_MACHINES:
                raise ValueError(f"coordinator supports at most {MAX_MACHINES} machines")
            if any(machine["name"].casefold() == name.casefold() for machine in machines):
                raise ValueError("machine name is already enrolled")
            if any(machine["origin"] == origin for machine in machines):
                raise ValueError("machine origin is already enrolled")
            machine = {"id": uuid.uuid4().hex, "name": name, "origin": origin}
            machines.append(machine)
            self._write_state()
            key = Ed25519PrivateKey.from_private_bytes(_b64decode(self._state["privateKey"], size=32))
            enrollment = {
                "version": 1,
                "machineId": machine["id"],
                "name": name,
                "coordinatorOrigin": self._coordinator_origin,
                "machineOrigin": origin,
                "publicKey": _b64encode(key.public_key().public_bytes_raw()),
            }
            return validate_enrollment(enrollment)

    def remove_machine(self, machine_id: str) -> dict:
        machine_id = validate_machine_id(machine_id)
        with self._locked_state():
            machines = self._state["machines"]
            found = next((machine for machine in machines if machine["id"] == machine_id), None)
            if found is None:
                raise ValueError("machine is not enrolled")
            self._state["machines"] = [machine for machine in machines if machine["id"] != machine_id]
            self._write_state()
            return dict(found)

    def validate_persisted(self) -> None:
        with self._locked_state():
            pass

    def _new_state(self) -> dict:
        key = Ed25519PrivateKey.generate()
        return {
            "version": STATE_VERSION,
            "privateKey": _b64encode(key.private_bytes_raw()),
            "machines": [],
        }

    def _validate_state(self, state) -> dict:
        if (
            not isinstance(state, dict)
            or set(state) != _STATE_KEYS
            or type(state.get("version")) is not int
            or state["version"] != STATE_VERSION
        ):
            raise ValueError("invalid coordinator state shape")
        _b64decode(state.get("privateKey"), size=32)
        machines = state.get("machines")
        if not isinstance(machines, list) or len(machines) > MAX_MACHINES:
            raise ValueError("invalid coordinator machines")
        names = set()
        origins = set()
        ids = set()
        for machine in machines:
            if not isinstance(machine, dict) or set(machine) != _MACHINE_KEYS:
                raise ValueError("invalid coordinator machine record")
            machine_id = validate_machine_id(machine.get("id"))
            name = validate_machine_name(machine.get("name"))
            origin = validate_origin(machine.get("origin"))
            folded_name = name.casefold()
            if machine_id in ids or folded_name in names or origin in origins:
                raise ValueError("duplicate coordinator machine record")
            ids.add(machine_id)
            names.add(folded_name)
            origins.add(origin)
            if machine != {"id": machine_id, "name": name, "origin": origin}:
                raise ValueError("non-canonical coordinator machine record")
        return state

    def _read_state(self, *, create: bool) -> dict:
        raw = self._storage.read(MAX_STATE_BYTES)
        if raw is None:
            if not create:
                raise CoordinatorStateError("coordinator state is missing")
            state = self._new_state()
            self._state = state
            self._write_state()
            return state
        if len(raw) > MAX_STATE_BYTES:
            raise CoordinatorStateError("coordinator state exceeds its size limit")
        try:
            return self._validate_state(json.loads(raw))
        except (UnicodeError, json.JSONDecodeError, RecursionError, TypeError, ValueError, KeyError) as error:
            raise CoordinatorStateError("coordinator state is corrupt") from error

    def _write_state(self) -> None:
        payload = (json.dumps(self._state, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        if len(payload) > MAX_STATE_BYTES:
            raise CoordinatorStateError("coordinator state exceeds its size limit")
        self._storage.replace(payload)

    @contextmanager
    def _locked_state(self, *, create: bool = False):
        with self._lock:
            try:
                with self._storage.locked():
                    self._state = self._read_state(create=create)
                    yield self._state
            except CoordinatorStateError:
                raise
            except OSError as error:
                raise CoordinatorStateError("coordinator state is unavailable or unsafe") from error
