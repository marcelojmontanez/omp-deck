from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from typing import Any

from .attachments import MAX_IMAGES, validated_image_content

SCHEMA_VERSION = 3
LEGACY_SCHEMA_VERSION = 2
TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
RECOVERY_ID = re.compile(r"^[a-f0-9]{64}$")


class RequestLedgerError(RuntimeError):
    pass


def validate_identity(scope: Any, request_id: Any, fingerprint: Any) -> None:
    if (
        not isinstance(scope, str)
        or len(scope) > 300
        or TOKEN.fullmatch(scope) is None
        or not isinstance(request_id, str)
        or len(request_id) > 128
        or TOKEN.fullmatch(request_id) is None
        or not isinstance(fingerprint, str)
        or re.fullmatch(r"[a-f0-9]{64}", fingerprint) is None
    ):
        raise RequestLedgerError("invalid request identity")


def validate_outcome(outcome: dict[str, Any], request_id: str) -> dict[str, Any]:
    allowed = {"requestId", "status", "errorCode", "result", "sessionId", "created", "acceptance"}
    if set(outcome) - allowed or outcome.get("requestId") != request_id:
        raise RequestLedgerError("invalid request outcome")
    status = outcome.get("status")
    if status not in {"accepted", "failed", "stale", "cancelled"}:
        raise RequestLedgerError("invalid request outcome")
    acceptance = outcome.get("acceptance")
    if acceptance is not None and (status != "accepted" or acceptance != "accepted"):
        raise RequestLedgerError("invalid request acceptance")
    error_code = outcome.get("errorCode")
    if error_code is not None and (
        not isinstance(error_code, str)
        or not error_code
        or len(error_code) > 128
        or TOKEN.fullmatch(error_code) is None
    ):
        raise RequestLedgerError("invalid request outcome")
    if "result" in outcome and outcome["result"] is not None and outcome["result"] != "stale":
        raise RequestLedgerError("invalid durable request result")
    session_id = outcome.get("sessionId")
    if session_id is not None and (
        not isinstance(session_id, str) or len(session_id) > 128 or TOKEN.fullmatch(session_id) is None
    ):
        raise RequestLedgerError("invalid request session")
    if "created" in outcome and not isinstance(outcome["created"], bool):
        raise RequestLedgerError("invalid request outcome")
    return copy.deepcopy(outcome)


def validate_recovery(recovery: dict[str, Any]) -> dict[str, Any]:
    required = {"kind", "sessionId", "action", "message", "images"}
    if set(recovery) != required or recovery.get("kind") != "message":
        raise RequestLedgerError("invalid request recovery")
    session_id = recovery.get("sessionId")
    if (
        not isinstance(session_id, str)
        or len(session_id) > 128
        or TOKEN.fullmatch(session_id) is None
        or recovery.get("action") not in {"sendMessage", "followUp"}
    ):
        raise RequestLedgerError("invalid request recovery")
    message = recovery.get("message")
    images = recovery.get("images")
    if not isinstance(message, str) or len(message) > 4_000:
        raise RequestLedgerError("invalid request recovery")
    if not isinstance(images, list) or len(images) > MAX_IMAGES or (not message and not images):
        raise RequestLedgerError("invalid request recovery")
    try:
        validated_image_content(images)
    except ValueError:
        raise RequestLedgerError("invalid request recovery") from None
    return copy.deepcopy(recovery)


def validate_store(data: Any, max_records: int) -> None:
    if not isinstance(data, dict) or set(data) != {"version", "records", "tombstones"}:
        raise RequestLedgerError("invalid request ledger")
    if data["version"] != SCHEMA_VERSION:
        raise RequestLedgerError("unsupported request ledger version")
    records = data["records"]
    tombstones = data["tombstones"]
    if (
        not isinstance(records, list)
        or len(records) > max_records
        or not isinstance(tombstones, list)
        or len(tombstones) > max_records
    ):
        raise RequestLedgerError("invalid request ledger")
    seen: set[tuple[str, str]] = set()
    for record in records:
        _validate_record(record, seen)
    for tombstone in tombstones:
        _validate_tombstone(tombstone, seen)


def validate_legacy_store(data: Any, max_records: int) -> None:
    if not isinstance(data, dict) or set(data) != {"version", "records"}:
        raise RequestLedgerError("invalid request ledger")
    if data["version"] != LEGACY_SCHEMA_VERSION:
        raise RequestLedgerError("unsupported request ledger version")
    records = data["records"]
    if not isinstance(records, list) or len(records) > max_records:
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
        _validate_common(record, seen)
        _validate_state_outcome(record)
        recovery = record["recovery"]
        if recovery is None:
            continue
        if not isinstance(recovery, dict):
            raise RequestLedgerError("invalid request ledger")
        allowed = {"kind", "sessionId", "action", "message", "images", "state", "errorCode"}
        if set(recovery) - allowed or "state" not in recovery:
            raise RequestLedgerError("invalid request ledger")
        base = {key: recovery[key] for key in ("kind", "sessionId", "action", "message", "images") if key in recovery}
        validate_recovery(base)
        _validate_recovery_state(recovery)


def capacity_projection(data: dict[str, Any]) -> dict[str, Any]:
    projected = copy.deepcopy(data)
    error_code = "x" * 128
    session_id = "x" * 128
    for record in projected["records"]:
        if record["state"] != "pending":
            continue
        record["state"] = "complete"
        record["outcome"] = {
            "requestId": record["requestId"],
            "status": "accepted",
            "errorCode": error_code,
            "result": "stale",
            "sessionId": session_id,
            "created": False,
            "acceptance": "accepted",
        }
        if record["recovery"] is not None:
            record["recovery"]["state"] = "cancelled"
            record["recovery"]["errorCode"] = error_code
    for tombstone in projected["tombstones"]:
        if tombstone["outcome"] is not None:
            continue
        tombstone["outcome"] = {
            "requestId": tombstone["requestId"],
            "status": "accepted",
            "errorCode": error_code,
            "result": "stale",
            "sessionId": session_id,
            "created": False,
            "acceptance": "accepted",
        }
    return projected


def _validate_record(record: Any, seen: set[tuple[str, str]]) -> None:
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
    _validate_common(record, seen)
    _validate_state_outcome(record)
    recovery = record["recovery"]
    if recovery is None:
        if record["state"] != "pending":
            raise RequestLedgerError("invalid request ledger")
        return
    _validate_recovery_metadata(recovery)


def _validate_tombstone(record: Any, seen: set[tuple[str, str]]) -> None:
    if not isinstance(record, dict) or set(record) != {
        "scope",
        "requestId",
        "fingerprint",
        "outcome",
        "createdAt",
        "updatedAt",
    }:
        raise RequestLedgerError("invalid request ledger")
    _validate_common(record, seen)
    outcome = record["outcome"]
    if outcome is not None:
        if not isinstance(outcome, dict):
            raise RequestLedgerError("invalid request ledger")
        validate_outcome(outcome, record["requestId"])


def _validate_common(record: Mapping[str, Any], seen: set[tuple[str, str]]) -> None:
    validate_identity(record.get("scope"), record.get("requestId"), record.get("fingerprint"))
    key = (record["scope"], record["requestId"])
    if (
        key in seen
        or not isinstance(record.get("createdAt"), int)
        or isinstance(record.get("createdAt"), bool)
        or not isinstance(record.get("updatedAt"), int)
        or isinstance(record.get("updatedAt"), bool)
        or record["createdAt"] < 0
        or record["updatedAt"] < record["createdAt"]
    ):
        raise RequestLedgerError("invalid request ledger")
    seen.add(key)


def _validate_state_outcome(record: Mapping[str, Any]) -> None:
    if record.get("state") not in {"pending", "complete"}:
        raise RequestLedgerError("invalid request ledger")
    if record["state"] == "pending":
        if record.get("outcome") is not None:
            raise RequestLedgerError("invalid request ledger")
    elif not isinstance(record.get("outcome"), dict):
        raise RequestLedgerError("invalid request ledger")
    else:
        validate_outcome(record["outcome"], record["requestId"])


def _validate_recovery_metadata(recovery: Any) -> None:
    if not isinstance(recovery, dict):
        raise RequestLedgerError("invalid request ledger")
    allowed = {"payload", "sessionId", "action", "state", "errorCode"}
    required = {"payload", "sessionId", "action", "state"}
    if set(recovery) - allowed or not required <= set(recovery):
        raise RequestLedgerError("invalid request ledger")
    if (
        not isinstance(recovery["payload"], str)
        or RECOVERY_ID.fullmatch(recovery["payload"]) is None
        or not isinstance(recovery["sessionId"], str)
        or len(recovery["sessionId"]) > 128
        or TOKEN.fullmatch(recovery["sessionId"]) is None
        or recovery["action"] not in {"sendMessage", "followUp"}
    ):
        raise RequestLedgerError("invalid request ledger")
    _validate_recovery_state(recovery)


def _validate_recovery_state(recovery: Mapping[str, Any]) -> None:
    if recovery.get("state") not in {"submitting", "uncertain", "accepted", "failed", "cancelled", "stale"}:
        raise RequestLedgerError("invalid request ledger")
    error_code = recovery.get("errorCode")
    if error_code is not None and (
        recovery["state"] not in {"uncertain", "failed", "cancelled", "stale"}
        or not isinstance(error_code, str)
        or not error_code
        or len(error_code) > 128
        or TOKEN.fullmatch(error_code) is None
    ):
        raise RequestLedgerError("invalid request ledger")
