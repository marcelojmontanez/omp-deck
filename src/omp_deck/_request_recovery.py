from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ._private_state import (
    PrivateStateFile,
    ensure_private_directory,
    validate_private_directory,
    validate_private_file,
)
from ._request_ledger_schema import RequestLedgerError, validate_recovery

_PAYLOAD_NAME = re.compile(r"([a-f0-9]{64})\.json")
_TEMPORARY_NAME = re.compile(r"\.([a-f0-9]{64})\.json\.[0-9a-f]{32}\.tmp")
_CLEANUP_BATCH = 4_096


class RecoveryPayloadStore:
    """Private sidecars for large delivery bodies, serialized by the request-ledger lock."""

    def __init__(self, ledger_path: Path, max_bytes: int) -> None:
        self.root = ledger_path.with_name(f".{ledger_path.name}.recoveries")
        self.max_bytes = max_bytes
        self._lock_name = ".store.lock"

    def validate_directory(self) -> int:
        validate_private_directory(self.root)
        count = 0
        with os.scandir(self.root) as entries:
            for entry in entries:
                count += 1
                if count > _CLEANUP_BATCH:
                    raise RequestLedgerError("request recovery store has too many entries")
                if entry.name == self._lock_name:
                    maximum = 0
                elif _PAYLOAD_NAME.fullmatch(entry.name) or _TEMPORARY_NAME.fullmatch(entry.name):
                    maximum = self.max_bytes
                else:
                    raise RequestLedgerError("request recovery store contains an unknown entry")
                validate_private_file(Path(entry.path), maximum)
        return count

    def put(self, payload_id: str, recovery: Mapping[str, Any]) -> None:
        ensure_private_directory(self.root)
        storage = self._storage(payload_id)
        encoded = json.dumps(recovery, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
        if len(encoded) > self.max_bytes:
            raise RequestLedgerError(
                "delivery recovery capacity is full; unresolved delivery receipts require attention"
            )
        with storage.locked():
            storage.cleanup_temporaries()
            current = storage.read(self.max_bytes)
            if current is not None:
                if current != encoded:
                    raise RequestLedgerError("request recovery payload conflicts with its durable identity")
                return
            storage.replace(encoded)

    def read(self, payload_id: str, metadata: Mapping[str, Any]) -> dict[str, Any]:
        storage = self._storage(payload_id)
        with storage.locked():
            storage.cleanup_temporaries()
            raw = storage.read(self.max_bytes)
        if raw is None:
            raise RequestLedgerError("request recovery payload is missing")
        if len(raw) > self.max_bytes:
            raise RequestLedgerError("request recovery payload is too large")
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
            raise RequestLedgerError("request recovery payload is corrupt") from error
        if not isinstance(value, dict):
            raise RequestLedgerError("request recovery payload is corrupt")
        recovery = validate_recovery(value)
        if recovery["sessionId"] != metadata["sessionId"] or recovery["action"] != metadata["action"]:
            raise RequestLedgerError("request recovery payload does not match its durable metadata")
        return recovery

    def measure(self, retained: set[str]) -> dict[str, int]:
        return self._inventory(retained, remove_unreferenced=False)

    def sweep(self, retained: set[str]) -> dict[str, int]:
        return self._inventory(retained, remove_unreferenced=True)

    def _inventory(self, retained: set[str], *, remove_unreferenced: bool) -> dict[str, int]:
        if not self.root.exists() and not self.root.is_symlink():
            if retained:
                raise RequestLedgerError("request recovery payload is missing")
            return {}
        validate_private_directory(self.root)
        guard = PrivateStateFile(self.root / ".guard", lock_name=self._lock_name)
        with guard.locked():
            pass
        sizes: dict[str, int] = {}
        for payload_id in retained:
            storage = self._storage(payload_id)
            with storage.locked():
                size = storage.size(self.max_bytes)
            if size is None:
                raise RequestLedgerError("request recovery payload is missing")
            sizes[payload_id] = size
        if not remove_unreferenced:
            return sizes
        try:
            entries = os.scandir(self.root)
        except OSError as error:
            raise RequestLedgerError("request recovery store is unavailable") from error
        with entries:
            for index, entry in enumerate(entries):
                if index >= _CLEANUP_BATCH:
                    break
                name = entry.name
                if name == self._lock_name:
                    continue
                payload_match = _PAYLOAD_NAME.fullmatch(name)
                if payload_match is not None:
                    payload_id = payload_match[1]
                    if payload_id not in retained:
                        storage = self._storage(payload_id)
                        with storage.locked():
                            storage.cleanup_temporaries()
                            storage.delete()
                    continue
                temporary_match = _TEMPORARY_NAME.fullmatch(name)
                if temporary_match is not None:
                    storage = self._storage(temporary_match[1])
                    with storage.locked():
                        storage.cleanup_temporaries()
                    continue
                raise RequestLedgerError("request recovery store contains an unknown entry")
        return sizes

    def _storage(self, payload_id: str) -> PrivateStateFile:
        if not isinstance(payload_id, str) or re.fullmatch(r"[a-f0-9]{64}", payload_id) is None:
            raise RequestLedgerError("invalid request recovery identity")
        return PrivateStateFile(self.root / f"{payload_id}.json", lock_name=self._lock_name)


def public_delivery(record: Mapping[str, Any], recovery: Mapping[str, Any], retention_ms: int) -> dict[str, Any]:
    metadata = record["recovery"]
    result = {
        "requestId": record["requestId"],
        "sessionId": metadata["sessionId"],
        "action": metadata["action"],
        "state": metadata["state"],
        "message": recovery["message"],
        "attachments": [
            {
                "name": f"Recovered image {index + 1}",
                "type": image["mimeType"],
                "data": f"data:{image['mimeType']};base64,{image['data']}",
            }
            for index, image in enumerate(recovery["images"])
        ],
        "createdAt": record["createdAt"],
        "updatedAt": record["updatedAt"],
        "expiresAt": record["createdAt"] + retention_ms,
    }
    if "errorCode" in metadata:
        result["errorCode"] = metadata["errorCode"]
    return result
