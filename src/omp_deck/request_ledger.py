from __future__ import annotations

import copy
import json
import os
import re
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ._private_state import PrivateStateFile
from .attachments import validated_image_content

_SCHEMA_VERSION = 2
_MAX_FILE_BYTES = 24 * 1024 * 1024
_RECOVERY_RETENTION_MS = 7 * 24 * 60 * 60 * 1_000
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


class RequestLedgerError(RuntimeError):
    pass


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

    def lookup(self, scope: str, request_id: str, fingerprint: str) -> dict[str, Any] | None:
        self._validate_identity(scope, request_id, fingerprint)
        with self._locked():
            data = self._read()
            record = self._find(data, scope, request_id)
            if record is None:
                return None
            if record["fingerprint"] != fingerprint:
                raise RequestConflictError("requestId was already used for another operation")
            if record["state"] == "pending":
                return {
                    "requestId": request_id,
                    "status": "uncertain",
                    "errorCode": "outcome_unknown",
                }
            return copy.deepcopy(record["outcome"])

    def reserve(
        self,
        scope: str,
        request_id: str,
        fingerprint: str,
        *,
        recovery: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        self._validate_identity(scope, request_id, fingerprint)
        recovery_record = self._validate_recovery(dict(recovery)) if recovery is not None else None
        with self._locked():
            data = self._read()
            now_ms = self._now_ms()
            self._prune_recoveries(data, now_ms)
            record = self._find(data, scope, request_id)
            if record is not None:
                if record["fingerprint"] != fingerprint:
                    raise RequestConflictError("requestId was already used for another operation")
                if record["state"] == "pending":
                    return {
                        "requestId": request_id,
                        "status": "uncertain",
                        "errorCode": "outcome_unknown",
                    }
                return copy.deepcopy(record["outcome"])
            if len(data["records"]) >= self.max_records:
                completed = next(
                    (
                        i
                        for i, item in enumerate(data["records"])
                        if item["state"] == "complete" and item["recovery"] is None
                    ),
                    None,
                )
                if completed is None:
                    raise RequestLedgerError("request ledger capacity is occupied by pending operations")
                data["records"].pop(completed)
            if recovery_record is not None:
                recovery_record["state"] = "submitting"
            data["records"].append(
                {
                    "scope": scope,
                    "requestId": request_id,
                    "fingerprint": fingerprint,
                    "state": "pending",
                    "outcome": None,
                    "createdAt": now_ms,
                    "updatedAt": now_ms,
                    "recovery": recovery_record,
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
        self._validate_identity(scope, request_id, fingerprint)
        durable = self._validate_outcome(dict(outcome), request_id)
        with self._locked():
            data = self._read()
            record = self._find(data, scope, request_id)
            if record is None:
                raise RequestLedgerError("request was not reserved")
            if record["fingerprint"] != fingerprint:
                raise RequestConflictError("requestId was already used for another operation")
            if record["state"] == "complete":
                if record["outcome"] != durable:
                    raise RequestLedgerError("request already has another outcome")
                return
            record["state"] = "complete"
            record["outcome"] = durable
            record["updatedAt"] = self._now_ms()
            recovery = record["recovery"]
            if recovery is not None:
                recovery["state"] = durable["status"]
                if durable.get("errorCode") is not None:
                    recovery["errorCode"] = durable["errorCode"]
            data["records"].remove(record)
            data["records"].append(record)
            self._write(data)

    def mark_uncertain(self, scope: str, request_id: str, fingerprint: str) -> None:
        self._validate_identity(scope, request_id, fingerprint)
        with self._locked():
            data = self._read()
            record = self._find(data, scope, request_id)
            if record is None:
                raise RequestLedgerError("request was not reserved")
            if record["fingerprint"] != fingerprint:
                raise RequestConflictError("requestId was already used for another operation")
            if record["state"] != "pending":
                return
            record["updatedAt"] = self._now_ms()
            if record["recovery"] is not None:
                record["recovery"]["state"] = "uncertain"
                record["recovery"]["errorCode"] = "outcome_unknown"
            self._write(data)

    def dismiss_delivery(self, session_id: str, request_id: str) -> bool:
        if (
            not isinstance(session_id, str)
            or not session_id
            or len(session_id) > 128
            or _TOKEN.fullmatch(session_id) is None
            or not isinstance(request_id, str)
            or not request_id
            or len(request_id) > 128
            or _TOKEN.fullmatch(request_id) is None
        ):
            raise RequestLedgerError("invalid delivery identity")
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
            record["recovery"] = None
            record["updatedAt"] = self._now_ms()
            self._write(data)
            return True

    def deliveries(self, session_id: str) -> list[dict[str, Any]]:
        if not isinstance(session_id, str) or not session_id or len(session_id) > 128:
            raise RequestLedgerError("invalid request session")
        with self._locked():
            data = self._read()
            if self._prune_recoveries(data, self._now_ms()):
                self._write(data)
            return [
                self._public_delivery(record)
                for record in data["records"]
                if record["recovery"] is not None and record["recovery"]["sessionId"] == session_id
            ]

    @staticmethod
    def _find(data: Mapping[str, Any], scope: str, request_id: str) -> dict[str, Any] | None:
        return next(
            (record for record in data["records"] if record["scope"] == scope and record["requestId"] == request_id),
            None,
        )

    @staticmethod
    def _validate_identity(scope: Any, request_id: Any, fingerprint: Any) -> None:
        if (
            not isinstance(scope, str)
            or len(scope) > 300
            or _TOKEN.fullmatch(scope) is None
            or not isinstance(request_id, str)
            or len(request_id) > 128
            or _TOKEN.fullmatch(request_id) is None
            or not isinstance(fingerprint, str)
            or re.fullmatch(r"[a-f0-9]{64}", fingerprint) is None
        ):
            raise RequestLedgerError("invalid request identity")

    @staticmethod
    def _validate_outcome(outcome: dict[str, Any], request_id: str) -> dict[str, Any]:
        allowed = {"requestId", "status", "errorCode", "result", "sessionId", "created", "acceptance"}
        if set(outcome) - allowed or outcome.get("requestId") != request_id:
            raise RequestLedgerError("invalid request outcome")
        status_value = outcome.get("status")
        if status_value not in {"accepted", "failed", "stale", "cancelled"}:
            raise RequestLedgerError("invalid request outcome")
        acceptance = outcome.get("acceptance")
        if acceptance is not None and (status_value != "accepted" or acceptance != "accepted"):
            raise RequestLedgerError("invalid request acceptance")
        error_code = outcome.get("errorCode")
        if error_code is not None and (
            not isinstance(error_code, str)
            or not error_code
            or len(error_code) > 128
            or _TOKEN.fullmatch(error_code) is None
        ):
            raise RequestLedgerError("invalid request outcome")
        if "result" in outcome and outcome["result"] is not None and outcome["result"] != "stale":
            raise RequestLedgerError("invalid durable request result")
        session_id = outcome.get("sessionId")
        if session_id is not None and (
            not isinstance(session_id, str) or len(session_id) > 128 or _TOKEN.fullmatch(session_id) is None
        ):
            raise RequestLedgerError("invalid request session")
        if "created" in outcome and not isinstance(outcome["created"], bool):
            raise RequestLedgerError("invalid request outcome")
        return copy.deepcopy(outcome)

    @staticmethod
    def _validate_recovery(recovery: dict[str, Any]) -> dict[str, Any]:
        required = {"kind", "sessionId", "action", "message", "images"}
        if set(recovery) != required or recovery.get("kind") != "message":
            raise RequestLedgerError("invalid request recovery")
        session_id = recovery.get("sessionId")
        if (
            not isinstance(session_id, str)
            or len(session_id) > 128
            or _TOKEN.fullmatch(session_id) is None
            or recovery.get("action") not in {"sendMessage", "followUp"}
        ):
            raise RequestLedgerError("invalid request recovery")
        message = recovery.get("message")
        images = recovery.get("images")
        if not isinstance(message, str) or len(message) > 4_000:
            raise RequestLedgerError("invalid request recovery")
        if not isinstance(images, list) or len(images) > 4 or (not message and not images):
            raise RequestLedgerError("invalid request recovery")
        try:
            validated_image_content(images)
        except ValueError:
            raise RequestLedgerError("invalid request recovery") from None
        return copy.deepcopy(recovery)

    def _now_ms(self) -> int:
        return int(self._clock() * 1_000)

    @staticmethod
    def _public_delivery(record: Mapping[str, Any]) -> dict[str, Any]:
        recovery = record["recovery"]
        result = {
            "requestId": record["requestId"],
            "sessionId": recovery["sessionId"],
            "action": recovery["action"],
            "state": recovery["state"],
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
            "expiresAt": record["createdAt"] + _RECOVERY_RETENTION_MS,
        }
        if "errorCode" in recovery:
            result["errorCode"] = recovery["errorCode"]
        return result

    @staticmethod
    def _prune_recoveries(data: dict[str, Any], now_ms: int) -> bool:
        changed = False
        for record in data["records"]:
            if record["recovery"] is not None and now_ms >= record["createdAt"] + _RECOVERY_RETENTION_MS:
                record["recovery"] = None
                changed = True
        return changed

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with self._thread_lock:
            with self._storage.locked():
                yield

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {"version": _SCHEMA_VERSION, "records": []}

    def _read(self) -> dict[str, Any]:
        raw = self._storage.read(_MAX_FILE_BYTES)
        if raw is None:
            return self._empty()
        if len(raw) > _MAX_FILE_BYTES:
            raise RequestLedgerError("request ledger is too large")
        try:
            data = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
            raise RequestLedgerError("request ledger is corrupt") from error
        if isinstance(data, dict) and data.get("version") == 1:
            now_ms = self._now_ms()
            records = data.get("records")
            if not isinstance(records, list):
                raise RequestLedgerError("invalid request ledger")
            data = {
                "version": _SCHEMA_VERSION,
                "records": [
                    {
                        **record,
                        "createdAt": now_ms,
                        "updatedAt": now_ms,
                        "recovery": None,
                    }
                    if isinstance(record, dict)
                    else record
                    for record in records
                ],
            }
        self._validate_store(data)
        return data

    def _validate_store(self, data: Any) -> None:
        if not isinstance(data, dict) or set(data) != {"version", "records"}:
            raise RequestLedgerError("invalid request ledger")
        if data["version"] != _SCHEMA_VERSION:
            raise RequestLedgerError("unsupported request ledger version")
        records = data["records"]
        if not isinstance(records, list) or len(records) > self.max_records:
            raise RequestLedgerError("invalid request ledger")
        seen: set[tuple[str, str]] = set()
        for record in records:
            if not isinstance(record, dict) or set(record) != {
                "scope",
                "requestId",
                "fingerprint",
                "state",
                "outcome",
                "createdAt",
                "updatedAt",
                "recovery",
            }:
                raise RequestLedgerError("invalid request ledger")
            self._validate_identity(record["scope"], record["requestId"], record["fingerprint"])
            key = (record["scope"], record["requestId"])
            if (
                key in seen
                or record["state"] not in {"pending", "complete"}
                or not isinstance(record["createdAt"], int)
                or isinstance(record["createdAt"], bool)
                or not isinstance(record["updatedAt"], int)
                or isinstance(record["updatedAt"], bool)
                or record["createdAt"] < 0
                or record["updatedAt"] < record["createdAt"]
            ):
                raise RequestLedgerError("invalid request ledger")
            seen.add(key)
            if record["state"] == "pending":
                if record["outcome"] is not None:
                    raise RequestLedgerError("invalid request ledger")
            elif not isinstance(record["outcome"], dict):
                raise RequestLedgerError("invalid request ledger")
            else:
                self._validate_outcome(record["outcome"], record["requestId"])
            recovery = record["recovery"]
            if recovery is None:
                continue
            if not isinstance(recovery, dict):
                raise RequestLedgerError("invalid request ledger")
            allowed = {
                "kind",
                "sessionId",
                "action",
                "message",
                "images",
                "state",
                "errorCode",
            }
            if set(recovery) - allowed or "state" not in recovery:
                raise RequestLedgerError("invalid request ledger")
            base = {
                key: recovery[key] for key in ("kind", "sessionId", "action", "message", "images") if key in recovery
            }
            self._validate_recovery(base)
            if recovery["state"] not in {
                "submitting",
                "uncertain",
                "accepted",
                "failed",
                "cancelled",
                "stale",
            }:
                raise RequestLedgerError("invalid request ledger")
            error_code = recovery.get("errorCode")
            if error_code is not None and (
                recovery["state"] not in {"uncertain", "failed", "cancelled", "stale"}
                or not isinstance(error_code, str)
                or not error_code
                or len(error_code) > 128
                or _TOKEN.fullmatch(error_code) is None
            ):
                raise RequestLedgerError("invalid request ledger")

    def _write(self, data: dict[str, Any]) -> None:
        self._validate_store(data)
        payload = json.dumps(
            data,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(payload) > _MAX_FILE_BYTES:
            raise RequestLedgerError("request ledger is too large")
        self._storage.replace(payload)
