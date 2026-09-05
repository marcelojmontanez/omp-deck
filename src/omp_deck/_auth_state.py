from __future__ import annotations

from typing import Any

MAX_STATE_BYTES = 1024 * 1024
MAX_PERSISTED_DEVICES = 1024


def validate(state: Any) -> None:
    if not isinstance(state, dict) or set(state) != {
        "version",
        "epoch",
        "pairing",
        "devices",
    }:
        raise ValueError("invalid auth state shape")
    if state["version"] != 1 or not isinstance(state["epoch"], str) or len(state["epoch"]) != 64:
        raise ValueError("unsupported auth state")
    devices = state["devices"]
    if not isinstance(devices, dict) or len(devices) > MAX_PERSISTED_DEVICES:
        raise ValueError("invalid devices")
    pairing = state["pairing"]
    if pairing is not None and (
        not isinstance(pairing, dict)
        or set(pairing) != {"verifier", "expiresAt"}
        or not isinstance(pairing["verifier"], str)
        or len(pairing["verifier"]) != 64
        or type(pairing["expiresAt"]) is not int
        or pairing["expiresAt"] < 0
    ):
        raise ValueError("invalid pairing verifier")
    for device_id, record in devices.items():
        if (
            not isinstance(device_id, str)
            or len(device_id) != 32
            or not isinstance(record, dict)
            or set(record) != {"verifier", "createdAt", "expiresAt", "epoch"}
            or not isinstance(record["verifier"], str)
            or len(record["verifier"]) != 64
            or type(record["createdAt"]) is not int
            or type(record["expiresAt"]) is not int
            or record["createdAt"] < 0
            or record["expiresAt"] < record["createdAt"]
            or not isinstance(record["epoch"], str)
            or len(record["epoch"]) != 64
        ):
            raise ValueError("invalid device record")
