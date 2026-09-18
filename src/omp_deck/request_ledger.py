from __future__ import annotations

import copy
import hashlib
import json
import os
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ._private_state import PrivateStateFile
from ._request_admission import RequestAdmissions
from ._request_ledger_schema import (
    LEGACY_SCHEMA_VERSION,
    SCHEMA_VERSION,
    TOKEN,
    RequestLedgerError,
    capacity_projection,
    validate_identity,
    validate_legacy_store,
    validate_outcome,
    validate_recovery,
    validate_store,
)
from ._request_recovery import RecoveryPayloadStore, public_delivery

_MAX_FILE_BYTES = 128 * 1024 * 1024
_MAX_RECOVERY_FILE_BYTES = 128 * 1024 * 1024
_MAX_METADATA_BYTES = 4 * 1024 * 1024
_RECOVERY_RETENTION_MS = 7 * 24 * 60 * 60 * 1_000


class RequestConflictError(RequestLedgerError, ValueError):
    pass


class RequestLedger:
    """Crash-safe request deduplication with bounded private delivery receipts."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        max_records: int = 1_000,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not isinstance(max_records, int) or isinstance(max_records, bool) or max_records < 1:
            raise ValueError("max_records must be a positive integer")
        self.path = Path(path)
        self.lock_path = self.path.with_name(f".{self.path.name}.lock")
        self.max_records = max_records
        self._clock = clock
        self._thread_lock = threading.RLock()
        self._storage = PrivateStateFile(self.path)
        self._payloads = RecoveryPayloadStore(self.path, _MAX_RECOVERY_FILE_BYTES)
        self._admissions = RequestAdmissions(self.path)
        self._admissions_swept = False

    @contextmanager
    def admission(self, scope: str, request_id: str, fingerprint: str, *, recovery=None):
        """Pin completion capacity for the entire native operation, not its HTTP client."""
        validate_identity(scope, request_id, fingerprint)
        token = self._payload_id(scope, request_id, fingerprint)
        with self._locked():
            lease = self._admissions.claim(token)
        try:
            if lease is None:
                yield self.lookup(scope, request_id, fingerprint) or {
                    "requestId": request_id,
                    "status": "uncertain",
                    "errorCode": "outcome_unknown",
                }
            else:
                yield self.reserve(scope, request_id, fingerprint, recovery=recovery)
        finally:
            if lease is not None:
                with self._locked():
                    self._admissions.release(token, lease)

    def validate_auxiliary_directory(self, path: Path) -> int | None:
        for store in (self._payloads, self._admissions):
            if path == store.root:
                return store.validate_directory()
        return None

    def _live(self, record: Mapping[str, Any]) -> bool:
        return record.get("outcome") is None and self._admissions.active(
            self._payload_id(record["scope"], record["requestId"], record["fingerprint"])
        )

    def lookup(self, scope: str, request_id: str, fingerprint: str) -> dict[str, Any] | None:
        validate_identity(scope, request_id, fingerprint)
        with self._locked():
            data = self._read()
            if self._prune(data, self._now_ms()):
                self._write(data)
            record = self._find(data, scope, request_id)
            if record is None:
                return None
            self._check_fingerprint(record, fingerprint)
            return self._durable_result(record)

    def reserve(
        self,
        scope: str,
        request_id: str,
        fingerprint: str,
        *,
        recovery: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        validate_identity(scope, request_id, fingerprint)
        recovery_record = validate_recovery(dict(recovery)) if recovery is not None else None
        with self._locked():
            data = self._read()
            changed = self._prune(data, self._now_ms())
            record = self._find(data, scope, request_id)
            if record is not None:
                self._check_fingerprint(record, fingerprint)
                if changed:
                    self._write(data)
                return self._durable_result(record)
            self._make_active_slot(data)
            now_ms = self._now_ms()
            recovery_metadata = None
            payload_id = None
            if recovery_record is not None:
                payload_id = self._payload_id(scope, request_id, fingerprint)
                self._payloads.put(payload_id, recovery_record)
                recovery_metadata = {
                    "payload": payload_id,
                    "sessionId": recovery_record["sessionId"],
                    "action": recovery_record["action"],
                    "state": "submitting",
                }
            data["records"].append(
                {
                    "scope": scope,
                    "requestId": request_id,
                    "fingerprint": fingerprint,
                    "state": "pending",
                    "outcome": None,
                    "createdAt": now_ms,
                    "updatedAt": now_ms,
                    "recovery": recovery_metadata,
                }
            )
            self._write(data)
            return None

    def complete(
        self,
        scope: str,
        request_id: str,
        fingerprint: str,
        outcome: Mapping[str, Any],
    ) -> None:
        validate_identity(scope, request_id, fingerprint)
        durable = validate_outcome(dict(outcome), request_id)
        with self._locked():
            data = self._read()
            record = self._find(data, scope, request_id)
            if record is None:
                raise RequestLedgerError("request was not reserved")
            self._check_fingerprint(record, fingerprint)
            current = record.get("outcome")
            if current is not None:
                if current == durable:
                    return
                raise RequestLedgerError("request already has another outcome")
            record["outcome"] = durable
            record["updatedAt"] = self._advanced_timestamp(record)
            if "state" in record:
                recovery = record["recovery"]
                if recovery is None:
                    self._retire(data, record)
                else:
                    record["state"] = "complete"
                    recovery["state"] = durable["status"]
                    if durable.get("errorCode") is None or durable["status"] == "accepted":
                        recovery.pop("errorCode", None)
                    else:
                        recovery["errorCode"] = durable["errorCode"]
                    data["records"].remove(record)
                    data["records"].append(record)
            self._write(data)

    def mark_uncertain(self, scope: str, request_id: str, fingerprint: str) -> None:
        validate_identity(scope, request_id, fingerprint)
        with self._locked():
            data = self._read()
            record = self._find(data, scope, request_id)
            if record is None:
                raise RequestLedgerError("request was not reserved")
            self._check_fingerprint(record, fingerprint)
            if record.get("outcome") is not None or "recovery" not in record:
                return
            record["updatedAt"] = self._advanced_timestamp(record)
            if record["recovery"] is not None:
                record["recovery"]["state"] = "uncertain"
                record["recovery"]["errorCode"] = "outcome_unknown"
            self._write(data)

    def dismiss_delivery(self, session_id: str, request_id: str) -> bool:
        self._validate_delivery_identity(session_id, request_id)
        with self._locked():
            data = self._read()
            record = next(
                (
                    item
                    for item in data["records"]
                    if item["requestId"] == request_id
                    and item["recovery"] is not None
                    and item["recovery"]["sessionId"] == session_id
                ),
                None,
            )
            if record is None:
                return False
            if self._live(record):
                raise RequestConflictError("delivery is still being submitted")
            record["updatedAt"] = self._advanced_timestamp(record)
            self._retire(data, record)
            self._write(data)
            return True

    def deliveries(self, session_id: str) -> list[dict[str, Any]]:
        if not isinstance(session_id, str) or not session_id or len(session_id) > 128:
            raise RequestLedgerError("invalid request session")
        with self._locked():
            data = self._read()
            if self._prune(data, self._now_ms()):
                self._write(data)
            result = []
            for record in data["records"]:
                metadata = record["recovery"]
                if metadata is None or metadata["sessionId"] != session_id:
                    continue
                recovery = self._payloads.read(metadata["payload"], metadata)
                delivery = public_delivery(record, recovery, _RECOVERY_RETENTION_MS)
                if record["outcome"] is None and not self._live(record):
                    delivery.update(state="uncertain", errorCode="outcome_unknown")
                result.append(delivery)
            return result

    @staticmethod
    def _find(data: Mapping[str, Any], scope: str, request_id: str) -> dict[str, Any] | None:
        for collection in (data["records"], data["tombstones"]):
            record = next(
                (item for item in collection if item["scope"] == scope and item["requestId"] == request_id),
                None,
            )
            if record is not None:
                return record
        return None

    @staticmethod
    def _check_fingerprint(record: Mapping[str, Any], fingerprint: str) -> None:
        if record["fingerprint"] != fingerprint:
            raise RequestConflictError("requestId was already used for another operation")

    @staticmethod
    def _durable_result(record: Mapping[str, Any]) -> dict[str, Any]:
        outcome = record.get("outcome")
        if outcome is not None:
            return copy.deepcopy(outcome)
        return {
            "requestId": record["requestId"],
            "status": "uncertain",
            "errorCode": "outcome_unknown",
        }

    @staticmethod
    def _validate_delivery_identity(session_id: Any, request_id: Any) -> None:
        if (
            not isinstance(session_id, str)
            or not session_id
            or len(session_id) > 128
            or TOKEN.fullmatch(session_id) is None
            or not isinstance(request_id, str)
            or not request_id
            or len(request_id) > 128
            or TOKEN.fullmatch(request_id) is None
        ):
            raise RequestLedgerError("invalid delivery identity")

    def _now_ms(self) -> int:
        return max(0, int(self._clock() * 1_000))

    def _advanced_timestamp(self, record: Mapping[str, Any]) -> int:
        return max(record["createdAt"], record["updatedAt"], self._now_ms())

    def _prune(self, data: dict[str, Any], now_ms: int) -> bool:
        expired = [
            record
            for record in data["records"]
            if now_ms >= record["createdAt"] + _RECOVERY_RETENTION_MS and not self._live(record)
        ]
        for record in expired:
            record["updatedAt"] = max(record["updatedAt"], record["createdAt"], now_ms)
            self._retire(data, record)
        return bool(expired)

    def _make_active_slot(self, data: dict[str, Any]) -> None:
        if len(data["records"]) < self.max_records:
            return
        candidate = next(
            (
                record
                for record in data["records"]
                if record["state"] == "complete" and record["outcome"]["status"] == "accepted"
            ),
            None,
        )
        if candidate is None:
            raise RequestLedgerError("request ledger capacity is occupied by pending or unresolved operations")
        self._retire(data, candidate)

    def _retire(self, data: dict[str, Any], record: dict[str, Any]) -> None:
        tombstone = {
            "scope": record["scope"],
            "requestId": record["requestId"],
            "fingerprint": record["fingerprint"],
            "outcome": copy.deepcopy(record.get("outcome")),
            "createdAt": record["createdAt"],
            "updatedAt": record["updatedAt"],
        }
        data["records"].remove(record)
        data["tombstones"].append(tombstone)
        if len(data["tombstones"]) > self.max_records:
            data["tombstones"].pop(0)

    @staticmethod
    def _payload_id(scope: str, request_id: str, fingerprint: str) -> str:
        identity = f"{scope}\0{request_id}\0{fingerprint}".encode()
        return hashlib.sha256(identity).hexdigest()

    @staticmethod
    def _recovery_ids(data: Mapping[str, Any]) -> set[str]:
        return {record["recovery"]["payload"] for record in data["records"] if record["recovery"] is not None}

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with self._thread_lock:
            with self._storage.locked():
                self._storage.cleanup_temporaries()
                if not self._admissions_swept:
                    self._admissions.sweep()
                    self._admissions_swept = True
                yield

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {"version": SCHEMA_VERSION, "records": [], "tombstones": []}

    def _read(self) -> dict[str, Any]:
        raw = self._storage.read(max(_MAX_FILE_BYTES, _MAX_METADATA_BYTES))
        if raw is None:
            data = self._empty()
            self._payloads.sweep(set())
            return data
        if len(raw) > max(_MAX_FILE_BYTES, _MAX_METADATA_BYTES):
            raise RequestLedgerError("request ledger is too large")
        try:
            data = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
            raise RequestLedgerError("request ledger is corrupt") from error
        if isinstance(data, dict) and data.get("version") == 1:
            data = self._upgrade_v1(data)
        if isinstance(data, dict) and data.get("version") == LEGACY_SCHEMA_VERSION:
            data = self._migrate_legacy(data)
            self._write(data)
            return data
        if len(raw) > _MAX_METADATA_BYTES:
            raise RequestLedgerError("request ledger metadata is too large")
        validate_store(data, self.max_records)
        self._payloads.sweep(self._recovery_ids(data))
        return data

    def _upgrade_v1(self, data: Mapping[str, Any]) -> dict[str, Any]:
        records = data.get("records")
        if not isinstance(records, list):
            raise RequestLedgerError("invalid request ledger")
        now_ms = self._now_ms()
        return {
            "version": LEGACY_SCHEMA_VERSION,
            "records": [
                {**record, "createdAt": now_ms, "updatedAt": now_ms, "recovery": None}
                if isinstance(record, dict)
                else record
                for record in records
            ],
        }

    def _migrate_legacy(self, data: dict[str, Any]) -> dict[str, Any]:
        validate_legacy_store(data, self.max_records)
        migrated = self._empty()
        for legacy in data["records"]:
            recovery = legacy["recovery"]
            if recovery is None:
                migrated["tombstones"].append(
                    {
                        "scope": legacy["scope"],
                        "requestId": legacy["requestId"],
                        "fingerprint": legacy["fingerprint"],
                        "outcome": copy.deepcopy(legacy["outcome"]),
                        "createdAt": legacy["createdAt"],
                        "updatedAt": legacy["updatedAt"],
                    }
                )
                continue
            base = {key: recovery[key] for key in ("kind", "sessionId", "action", "message", "images")}
            payload_id = self._payload_id(legacy["scope"], legacy["requestId"], legacy["fingerprint"])
            self._payloads.put(payload_id, base)
            metadata = {
                "payload": payload_id,
                "sessionId": base["sessionId"],
                "action": base["action"],
                "state": recovery["state"],
            }
            if "errorCode" in recovery:
                metadata["errorCode"] = recovery["errorCode"]
            migrated["records"].append(
                {
                    "scope": legacy["scope"],
                    "requestId": legacy["requestId"],
                    "fingerprint": legacy["fingerprint"],
                    "state": legacy["state"],
                    "outcome": copy.deepcopy(legacy["outcome"]),
                    "createdAt": legacy["createdAt"],
                    "updatedAt": legacy["updatedAt"],
                    "recovery": metadata,
                }
            )
        validate_store(migrated, self.max_records)
        return migrated

    def _write(self, data: dict[str, Any]) -> None:
        validate_store(data, self.max_records)
        retained = self._recovery_ids(data)
        sizes = self._payloads.measure(retained)
        while sum(sizes[payload_id] for payload_id in retained) > _MAX_FILE_BYTES:
            candidate = next(
                (
                    record
                    for record in data["records"]
                    if record["state"] == "complete" and record["outcome"]["status"] == "accepted"
                ),
                None,
            )
            if candidate is None:
                raise RequestLedgerError(
                    "delivery recovery capacity is full; unresolved delivery receipts require attention"
                )
            self._retire(data, candidate)
            retained = self._recovery_ids(data)
        validate_store(data, self.max_records)
        payload = self._encode(data)
        capacity_payload = self._encode(capacity_projection(data))
        if len(payload) > _MAX_METADATA_BYTES or len(capacity_payload) > _MAX_METADATA_BYTES:
            raise RequestLedgerError("request ledger metadata capacity is full")
        self._storage.replace(payload)
        self._payloads.sweep(retained)

    @staticmethod
    def _encode(data: Mapping[str, Any]) -> bytes:
        return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
