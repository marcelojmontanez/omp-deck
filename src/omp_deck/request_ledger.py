from __future__ import annotations

import copy
import fcntl
import json
import os
import re
import stat
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

_SCHEMA_VERSION = 1
_MAX_FILE_BYTES = 2 * 1024 * 1024
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


class RequestLedgerError(RuntimeError):
    pass


class RequestConflictError(RequestLedgerError, ValueError):
    pass


class RequestLedger:
    """Crash-safe request reservations that prevent ambiguous operation replay."""

    def __init__(self, path: str | os.PathLike[str], *, max_records: int = 1_000) -> None:
        if not isinstance(max_records, int) or isinstance(max_records, bool) or max_records < 1:
            raise ValueError("max_records must be a positive integer")
        self.path = Path(path)
        self.lock_path = self.path.with_name(f".{self.path.name}.lock")
        self.max_records = max_records
        self._thread_lock = threading.RLock()

    def reserve(self, scope: str, request_id: str, fingerprint: str) -> dict[str, Any] | None:
        self._validate_identity(scope, request_id, fingerprint)
        with self._locked():
            data = self._read()
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
                    (index for index, item in enumerate(data["records"]) if item["state"] == "complete"),
                    None,
                )
                if completed is None:
                    raise RequestLedgerError("request ledger capacity is occupied by pending operations")
                data["records"].pop(completed)
            data["records"].append(
                {
                    "scope": scope,
                    "requestId": request_id,
                    "fingerprint": fingerprint,
                    "state": "pending",
                    "outcome": None,
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
            data["records"].remove(record)
            data["records"].append(record)
            self._write(data)

    @staticmethod
    def _find(data: Mapping[str, Any], scope: str, request_id: str) -> dict[str, Any] | None:
        return next(
            (
                record
                for record in data["records"]
                if record["scope"] == scope and record["requestId"] == request_id
            ),
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
        allowed = {"requestId", "status", "errorCode", "result", "sessionId", "created"}
        if set(outcome) - allowed or outcome.get("requestId") != request_id:
            raise RequestLedgerError("invalid request outcome")
        status_value = outcome.get("status")
        if status_value not in {"accepted", "failed", "stale", "cancelled"}:
            raise RequestLedgerError("invalid request outcome")
        error_code = outcome.get("errorCode")
        if error_code is not None and (
            not isinstance(error_code, str)
            or not error_code
            or len(error_code) > 128
            or _TOKEN.fullmatch(error_code) is None
        ):
            raise RequestLedgerError("invalid request outcome")
        if (
            "result" in outcome
            and outcome["result"] is not None
            and outcome["result"] != "stale"
        ):
            raise RequestLedgerError("invalid durable request result")
        session_id = outcome.get("sessionId")
        if session_id is not None and (
            not isinstance(session_id, str)
            or len(session_id) > 128
            or _TOKEN.fullmatch(session_id) is None
        ):
            raise RequestLedgerError("invalid request session")
        if "created" in outcome and not isinstance(outcome["created"], bool):
            raise RequestLedgerError("invalid request outcome")
        return copy.deepcopy(outcome)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with self._thread_lock:
            self._ensure_root()
            directory_fd = os.open(
                self.path.parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                descriptor = os.open(
                    self.lock_path.name,
                    os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=directory_fd,
                )
                try:
                    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                        raise OSError("request ledger lock is not a regular file")
                    os.fchmod(descriptor, 0o600)
                    fcntl.flock(descriptor, fcntl.LOCK_EX)
                    yield
                finally:
                    os.close(descriptor)
            finally:
                os.close(directory_fd)

    def _ensure_root(self) -> None:
        try:
            info = self.path.parent.lstat()
        except FileNotFoundError:
            self.path.parent.mkdir(parents=True, mode=0o700)
            info = self.path.parent.lstat()
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
        ):
            raise OSError("request ledger root is unsafe")
        os.chmod(self.path.parent, 0o700)

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {"version": _SCHEMA_VERSION, "records": []}

    def _read(self) -> dict[str, Any]:
        directory_fd = os.open(
            self.path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            try:
                descriptor = os.open(
                    self.path.name,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=directory_fd,
                )
            except FileNotFoundError:
                return self._empty()
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise OSError("request ledger is not a regular file")
                raw = os.read(descriptor, _MAX_FILE_BYTES + 1)
                os.fchmod(descriptor, 0o600)
            finally:
                os.close(descriptor)
        finally:
            os.close(directory_fd)
        if len(raw) > _MAX_FILE_BYTES:
            raise RequestLedgerError("request ledger is too large")
        try:
            data = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
            raise RequestLedgerError("request ledger is corrupt") from error
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
                "scope", "requestId", "fingerprint", "state", "outcome"
            }:
                raise RequestLedgerError("invalid request ledger")
            self._validate_identity(record["scope"], record["requestId"], record["fingerprint"])
            key = (record["scope"], record["requestId"])
            if key in seen or record["state"] not in {"pending", "complete"}:
                raise RequestLedgerError("invalid request ledger")
            seen.add(key)
            if record["state"] == "pending":
                if record["outcome"] is not None:
                    raise RequestLedgerError("invalid request ledger")
            elif not isinstance(record["outcome"], dict):
                raise RequestLedgerError("invalid request ledger")
            else:
                self._validate_outcome(record["outcome"], record["requestId"])

    def _write(self, data: dict[str, Any]) -> None:
        self._validate_store(data)
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(payload) > _MAX_FILE_BYTES:
            raise RequestLedgerError("request ledger is too large")
        temporary_name = f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        directory_fd = os.open(
            self.path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        descriptor = -1
        try:
            descriptor = os.open(
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=directory_fd,
            )
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(
                temporary_name,
                self.path.name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            os.fsync(directory_fd)
        except BaseException:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
            raise
        finally:
            os.close(directory_fd)
