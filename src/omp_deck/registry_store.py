"""Durable storage for Deck's non-secret session registry."""

from __future__ import annotations

import copy
import fcntl
import json
import math
import os
import re
import stat
import threading
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ._atomic_file import replace_bytes

SCHEMA_VERSION = 1
DEFAULT_MAX_RECORDS = 1_000
_ADAPTER_KINDS = frozenset({"rpc", "collab", "extension"})
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_FORBIDDEN_METADATA_MARKERS = (
    "authorization",
    "content",
    "cookie",
    "credential",
    "message",
    "password",
    "secret",
    "token",
    "transcript",
)
_RECORD_KEYS = frozenset(
    {
        "id",
        "adapterKind",
        "ompSessionId",
        "project",
        "title",
        "desiredRpcRunning",
        "recoveryMetadata",
        "generation",
        "createdAt",
        "updatedAt",
    }
)
_UNSET = object()


class RegistryError(Exception):
    """Base class for registry failures."""


class RegistryValidationError(RegistryError, ValueError):
    """A requested or persisted registry value violates the schema."""


class RegistryCapacityError(RegistryError):
    """The configured record bound has been reached."""


class RegistryCorruptionError(RegistryError):
    """The persisted registry was invalid and moved aside for recovery."""

    def __init__(self, message: str, quarantine_path: Path):
        super().__init__(message)
        self.quarantine_path = quarantine_path


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _normalized_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def _validate_text(value: Any, name: str, *, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 4096 or "\x00" in value:
        raise RegistryValidationError(f"{name} must be a non-empty bounded string")
    return value


def _validate_safe_metadata(value: Any, *, depth: int = 0) -> None:
    if depth > 8:
        raise RegistryValidationError("recoveryMetadata is too deeply nested")
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise RegistryValidationError("recoveryMetadata numbers must be finite")
        return
    if isinstance(value, str):
        if len(value) > 16_384 or "\x00" in value:
            raise RegistryValidationError("recoveryMetadata contains an invalid string")
        return
    if isinstance(value, list):
        if len(value) > 256:
            raise RegistryValidationError("recoveryMetadata list is too large")
        for item in value:
            _validate_safe_metadata(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > 256:
            raise RegistryValidationError("recoveryMetadata object is too large")
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > 128:
                raise RegistryValidationError("recoveryMetadata keys must be bounded strings")
            normalized = _normalized_key(key)
            if any(marker in normalized for marker in _FORBIDDEN_METADATA_MARKERS) or normalized.endswith(
                ("link", "url")
            ):
                raise RegistryValidationError(f"recoveryMetadata field {key!r} is secret or unsafe")
            _validate_safe_metadata(item, depth=depth + 1)
        return
    raise RegistryValidationError("recoveryMetadata must contain only JSON values")


class RegistryStore:
    """Versioned, bounded and atomic persistent session registry.

    The in-process lock protects threads, while the adjacent advisory lock avoids
    lost updates when multiple store instances address the same registry.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        max_records: int = DEFAULT_MAX_RECORDS,
        clock: Callable[[], datetime | str] = _utc_now,
        id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
    ) -> None:
        if not isinstance(max_records, int) or isinstance(max_records, bool) or max_records < 1:
            raise ValueError("max_records must be a positive integer")
        self.path = Path(path)
        self.max_records = max_records
        self._clock = clock
        self._id_factory = id_factory
        self._lock = threading.RLock()
        self._lock_path = self.path.with_name(f".{self.path.name}.lock")

    def load(self) -> list[dict[str, Any]]:
        """Return an isolated snapshot ordered by creation/insertion order."""
        with self._locked():
            data = self._read_or_quarantine()
            return copy.deepcopy(data["sessions"])

    def create(
        self,
        *,
        adapter_kind: str,
        project_name: str,
        project_path: str,
        title: str,
        omp_session_id: str | None = None,
        desired_rpc_running: bool = False,
        recovery_metadata: Mapping[str, Any] | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        with self._locked():
            data = self._read_or_quarantine()
            if len(data["sessions"]) >= self.max_records:
                raise RegistryCapacityError(f"registry is limited to {self.max_records} records")
            candidate_id = session_id if session_id is not None else self._new_unique_id(data)
            now = self._timestamp()
            record = {
                "id": candidate_id,
                "adapterKind": adapter_kind,
                "ompSessionId": omp_session_id,
                "project": {"name": project_name, "path": project_path},
                "title": title,
                "desiredRpcRunning": desired_rpc_running,
                "recoveryMetadata": dict(recovery_metadata or {}),
                "generation": data["generation"] + 1,
                "createdAt": now,
                "updatedAt": now,
            }
            self._validate_record(record)
            if any(existing["id"] == candidate_id for existing in data["sessions"]):
                raise RegistryValidationError(f"duplicate session id: {candidate_id}")
            data["generation"] += 1
            data["sessions"].append(record)
            self._write(data)
            return copy.deepcopy(record)

    def update(
        self,
        session_id: str,
        *,
        adapter_kind: str | object = _UNSET,
        project_name: str | object = _UNSET,
        project_path: str | object = _UNSET,
        title: str | object = _UNSET,
        omp_session_id: str | None | object = _UNSET,
        desired_rpc_running: bool | object = _UNSET,
        recovery_metadata: Mapping[str, Any] | object = _UNSET,
    ) -> dict[str, Any]:
        with self._locked():
            data = self._read_or_quarantine()
            record = next((item for item in data["sessions"] if item["id"] == session_id), None)
            if record is None:
                raise KeyError(session_id)
            if adapter_kind is not _UNSET:
                record["adapterKind"] = adapter_kind
            if project_name is not _UNSET:
                record["project"]["name"] = project_name
            if project_path is not _UNSET:
                record["project"]["path"] = project_path
            if title is not _UNSET:
                record["title"] = title
            if omp_session_id is not _UNSET:
                record["ompSessionId"] = omp_session_id
            if desired_rpc_running is not _UNSET:
                record["desiredRpcRunning"] = desired_rpc_running
            if recovery_metadata is not _UNSET:
                if not isinstance(recovery_metadata, Mapping):
                    raise RegistryValidationError("recoveryMetadata must be an object")
                record["recoveryMetadata"] = dict(recovery_metadata)
            next_generation = data["generation"] + 1
            record["generation"] = next_generation
            record["updatedAt"] = self._timestamp()
            self._validate_record(record)
            data["generation"] = next_generation
            self._write(data)
            return copy.deepcopy(record)

    def remove(self, session_id: str) -> bool:
        with self._locked():
            data = self._read_or_quarantine()
            remaining = [record for record in data["sessions"] if record["id"] != session_id]
            if len(remaining) == len(data["sessions"]):
                return False
            data["sessions"] = remaining
            data["generation"] += 1
            self._write(data)
            return True

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with self._lock:
            self._ensure_parent()
            directory_fd = os.open(
                self.path.parent,
                _DIRECTORY_FLAGS,
            )
            try:
                try:
                    fd = os.open(
                        self._lock_path.name,
                        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                        0o600,
                        dir_fd=directory_fd,
                    )
                except OSError as error:
                    raise RegistryError("cannot safely open registry lock") from error
                try:
                    if not stat.S_ISREG(os.fstat(fd).st_mode):
                        raise RegistryError("registry lock is not a regular file")
                    os.fchmod(fd, 0o600)
                    fcntl.flock(fd, fcntl.LOCK_EX)
                    yield
                finally:
                    os.close(fd)
            finally:
                os.close(directory_fd)

    def _ensure_parent(self) -> None:
        try:
            info = self.path.parent.lstat()
        except FileNotFoundError:
            self.path.parent.mkdir(parents=True, mode=0o700)
            info = self.path.parent.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise RegistryError("registry root is unsafe")
        os.chmod(self.path.parent, 0o700)

    def _empty(self) -> dict[str, Any]:
        return {"version": SCHEMA_VERSION, "generation": 0, "sessions": []}

    def _read_or_quarantine(self) -> dict[str, Any]:
        directory_fd = os.open(
            self.path.parent,
            _DIRECTORY_FLAGS,
        )
        try:
            try:
                fd = os.open(
                    self.path.name,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=directory_fd,
                )
            except FileNotFoundError:
                return self._empty()
            except OSError as error:
                raise RegistryError("cannot safely open registry") from error
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise RegistryError("registry is not a regular file")
                raw = os.read(fd, 8 * 1024 * 1024 + 1)
                os.fchmod(fd, 0o600)
            finally:
                os.close(fd)
        finally:
            os.close(directory_fd)
        try:
            if len(raw) > 8 * 1024 * 1024:
                raise RegistryValidationError("registry file is too large")
            data = json.loads(raw)
            self._validate_store(data)
            return data
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            RecursionError,
            RegistryValidationError,
        ) as error:
            quarantine = self._quarantine()
            raise RegistryCorruptionError(f"invalid registry quarantined at {quarantine}", quarantine) from error

    def _validate_store(self, data: Any) -> None:
        if not isinstance(data, dict) or set(data) != {"version", "generation", "sessions"}:
            raise RegistryValidationError("invalid registry envelope")
        if data["version"] != SCHEMA_VERSION:
            raise RegistryValidationError("unsupported registry version")
        if not isinstance(data["generation"], int) or isinstance(data["generation"], bool) or data["generation"] < 0:
            raise RegistryValidationError("invalid registry generation")
        sessions = data["sessions"]
        if not isinstance(sessions, list) or len(sessions) > self.max_records:
            raise RegistryValidationError("invalid registry session count")
        seen: set[str] = set()
        max_generation = 0
        for record in sessions:
            self._validate_record(record)
            if record["id"] in seen:
                raise RegistryValidationError("duplicate persisted session id")
            seen.add(record["id"])
            max_generation = max(max_generation, record["generation"])
        if max_generation > data["generation"]:
            raise RegistryValidationError("record generation exceeds registry generation")

    def _validate_record(self, record: Any) -> None:
        if not isinstance(record, dict) or set(record) != _RECORD_KEYS:
            raise RegistryValidationError("invalid registry record shape")
        session_id = _validate_text(record["id"], "id")
        if not _ID_RE.fullmatch(session_id):
            raise RegistryValidationError("id contains unsupported characters")
        if record["adapterKind"] not in _ADAPTER_KINDS:
            raise RegistryValidationError("invalid adapterKind")
        _validate_text(record["ompSessionId"], "ompSessionId", nullable=True)
        project = record["project"]
        if not isinstance(project, dict) or set(project) != {"name", "path"}:
            raise RegistryValidationError("invalid project")
        _validate_text(project["name"], "project.name")
        _validate_text(project["path"], "project.path")
        _validate_text(record["title"], "title")
        if not isinstance(record["desiredRpcRunning"], bool):
            raise RegistryValidationError("desiredRpcRunning must be boolean")
        if not isinstance(record["recoveryMetadata"], dict):
            raise RegistryValidationError("recoveryMetadata must be an object")
        _validate_safe_metadata(record["recoveryMetadata"])
        if (
            not isinstance(record["generation"], int)
            or isinstance(record["generation"], bool)
            or record["generation"] < 1
        ):
            raise RegistryValidationError("invalid record generation")
        for field in ("createdAt", "updatedAt"):
            timestamp = _validate_text(record[field], field)
            try:
                parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            except ValueError as exc:
                raise RegistryValidationError(f"invalid {field}") from exc
            if parsed.tzinfo is None:
                raise RegistryValidationError(f"{field} must include a timezone")

    def _timestamp(self) -> str:
        value = self._clock()
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=UTC)
            return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise RegistryValidationError("clock returned an invalid timestamp") from exc
            if parsed.tzinfo is None:
                raise RegistryValidationError("clock timestamp must include a timezone")
            return value
        raise RegistryValidationError("clock must return datetime or RFC3339 string")

    def _new_unique_id(self, data: dict[str, Any]) -> str:
        existing = {record["id"] for record in data["sessions"]}
        for _ in range(128):
            candidate = self._id_factory()
            if isinstance(candidate, str) and _ID_RE.fullmatch(candidate) and candidate not in existing:
                return candidate
        raise RegistryValidationError("id factory did not produce a unique valid id")

    def _quarantine(self) -> Path:
        stamp = re.sub(r"[^0-9]", "", self._timestamp()) or "unknown"
        base_name = f"{self.path.name}.corrupt.{stamp}"
        quarantine_name = base_name
        suffix = 0
        directory_fd = os.open(
            self.path.parent,
            _DIRECTORY_FLAGS,
        )
        try:
            while True:
                try:
                    os.stat(
                        quarantine_name,
                        dir_fd=directory_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    break
                suffix += 1
                quarantine_name = f"{base_name}.{suffix}"
            os.replace(
                self.path.name,
                quarantine_name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            fd = os.open(
                quarantine_name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_fd,
            )
            try:
                os.fchmod(fd, 0o600)
            finally:
                os.close(fd)
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return self.path.parent / quarantine_name

    def _write(self, data: dict[str, Any]) -> None:
        self._validate_store(data)
        payload = json.dumps(
            data,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(payload) > 8 * 1024 * 1024:
            raise RegistryValidationError("registry file is too large")
        directory_fd = os.open(self.path.parent, _DIRECTORY_FLAGS)
        try:
            replace_bytes(directory_fd, self.path.name, payload)
        finally:
            os.close(directory_fd)
