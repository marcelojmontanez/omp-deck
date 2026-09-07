"""Protected persistence for writable OMP Collab credentials."""

from __future__ import annotations

import copy
import json
import os
import re
import threading
import unicodedata
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ._private_state import PrivateStateFile
from .collab_protocol import parse_collab_link

SCHEMA_VERSION = 1
DEFAULT_MAX_RECORDS = 1_000
MAX_LINK_LENGTH = 8_192
MAX_DESCRIPTOR_LENGTH = 2_048
MAX_FILE_BYTES = 16 * 1024 * 1024
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_RECORD_KEYS = frozenset({"sessionId", "link", "relay", "room", "createdAt", "updatedAt"})
_PUBLIC_KEYS = ("sessionId", "createdAt", "updatedAt")
_MAX_JSON_DEPTH = 64


class CollabStoreError(Exception):
    """Base class for credential-store failures."""


class CollabStoreValidationError(CollabStoreError, ValueError):
    """A value does not satisfy the credential-store schema."""


class CollabStoreCapacityError(CollabStoreError):
    """The configured record limit has been reached."""


class CollabStoreCorruptionError(CollabStoreError):
    """Persisted credentials were invalid and have been quarantined."""

    def __init__(self, quarantine_path: Path):
        super().__init__("invalid Collab credential store was quarantined")
        self.quarantine_path = quarantine_path


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _field(parsed: Any, *names: str) -> Any:
    if isinstance(parsed, Mapping):
        for name in names:
            if name in parsed:
                return parsed[name]
    for name in names:
        if hasattr(parsed, name):
            return getattr(parsed, name)
    return None


class CollabStore:
    """Versioned atomic store whose normal projections never expose credentials."""

    def __init__(
        self,
        state_root: str | os.PathLike[str],
        *,
        max_records: int = DEFAULT_MAX_RECORDS,
        clock: Callable[[], datetime | str] = _utc_now,
    ) -> None:
        if not isinstance(max_records, int) or isinstance(max_records, bool) or max_records < 1:
            raise ValueError("max_records must be a positive integer")
        self.state_root = Path(state_root)
        self.path = self.state_root / "collab.json"
        self._lock_path = self.state_root / ".collab.json.lock"
        self.max_records = max_records
        self._clock = clock
        self._thread_lock = threading.RLock()
        self._storage = PrivateStateFile(self.path)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(state_root={self.state_root!r}, max_records={self.max_records!r})"

    def list(self) -> list[dict[str, str]]:
        with self._locked():
            data = self._read_or_quarantine()
            return [self._public(record) for record in data["records"]]

    def get(self, session_id: str) -> dict[str, str] | None:
        self._validate_session_id(session_id)
        with self._locked():
            record = self._find(self._read_or_quarantine(), session_id)
            return None if record is None else self._public(record)

    def get_link(self, session_id: str) -> str | None:
        """Explicitly retrieve the secret writable link for internal transport use."""
        self._validate_session_id(session_id)
        with self._locked():
            record = self._find(self._read_or_quarantine(), session_id)
            return None if record is None else record["link"]

    def put(self, session_id: str, link: str) -> dict[str, str]:
        """Create or replace one session credential and return only its safe view."""
        self._validate_session_id(session_id)
        relay, room = self._validate_link(link)
        with self._locked():
            data = self._read_or_quarantine()
            record = self._find(data, session_id)
            now = self._timestamp()
            if record is None:
                if len(data["records"]) >= self.max_records:
                    raise CollabStoreCapacityError(f"credential store is limited to {self.max_records} records")
                record = {
                    "sessionId": session_id,
                    "link": link,
                    "relay": relay,
                    "room": room,
                    "createdAt": now,
                    "updatedAt": now,
                }
                data["records"].append(record)
            else:
                record.update(link=link, relay=relay, room=room, updatedAt=now)
            self._write(data)
            return self._public(record)

    set_link = put

    def remove(self, session_id: str) -> bool:
        self._validate_session_id(session_id)
        with self._locked():
            data = self._read_or_quarantine()
            remaining = [r for r in data["records"] if r["sessionId"] != session_id]
            if len(remaining) == len(data["records"]):
                return False
            data["records"] = remaining
            self._write(data)
            return True

    @staticmethod
    def _public(record: Mapping[str, str]) -> dict[str, str]:
        return copy.deepcopy({key: record[key] for key in _PUBLIC_KEYS})

    @staticmethod
    def _find(data: Mapping[str, Any], session_id: str) -> dict[str, str] | None:
        return next((record for record in data["records"] if record["sessionId"] == session_id), None)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with self._thread_lock:
            with self._storage.locked():
                yield

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {"version": SCHEMA_VERSION, "records": []}

    def _read_or_quarantine(self) -> dict[str, Any]:
        try:
            raw = self._storage.read(MAX_FILE_BYTES)
            if raw is None:
                return self._empty()
            self._validate_json_depth(raw)
            data = json.loads(raw)
            self._validate_store(data)
            return data
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError, CollabStoreValidationError):
            quarantine = self._quarantine()
            raise CollabStoreCorruptionError(quarantine) from None

    @staticmethod
    def _validate_json_depth(raw: bytes) -> None:
        depth = 0
        in_string = escaped = False
        for byte in raw:
            if in_string:
                if escaped:
                    escaped = False
                elif byte == 0x5C:
                    escaped = True
                elif byte == 0x22:
                    in_string = False
            elif byte == 0x22:
                in_string = True
            elif byte in (0x5B, 0x7B):
                depth += 1
                if depth > _MAX_JSON_DEPTH:
                    raise CollabStoreValidationError("credential store nesting is too deep")
            elif byte in (0x5D, 0x7D):
                depth -= 1

    def _validate_store(self, data: Any) -> None:
        if not isinstance(data, dict) or set(data) != {"version", "records"}:
            raise CollabStoreValidationError("invalid credential store envelope")
        if data["version"] != SCHEMA_VERSION:
            raise CollabStoreValidationError("unsupported credential store version")
        records = data["records"]
        if not isinstance(records, list) or len(records) > self.max_records:
            raise CollabStoreValidationError("invalid credential record count")
        seen: set[str] = set()
        for record in records:
            self._validate_record(record)
            if record["sessionId"] in seen:
                raise CollabStoreValidationError("duplicate persisted session id")
            seen.add(record["sessionId"])

    def _validate_record(self, record: Any) -> None:
        if not isinstance(record, dict) or set(record) != _RECORD_KEYS:
            raise CollabStoreValidationError("invalid credential record shape")
        self._validate_session_id(record["sessionId"])
        relay, room = self._validate_link(record["link"])
        if record["relay"] != relay or record["room"] != room:
            raise CollabStoreValidationError("credential descriptors do not match link")
        for name in ("relay", "room"):
            self._validate_descriptor(record[name], name)
        for name in ("createdAt", "updatedAt"):
            self._validate_timestamp(record[name], name)

    @staticmethod
    def _validate_session_id(value: Any) -> None:
        if not isinstance(value, str) or not _ID_RE.fullmatch(value) or CollabStore._has_unsafe_unicode(value):
            raise CollabStoreValidationError("session id contains unsupported characters")

    def _validate_link(self, link: Any) -> tuple[str, str]:
        if not isinstance(link, str) or not link or len(link) > MAX_LINK_LENGTH or self._has_unsafe_unicode(link):
            raise CollabStoreValidationError("Collab link must be a non-empty bounded string")
        try:
            parsed = parse_collab_link(link)
        except Exception:
            raise CollabStoreValidationError("invalid writable Collab link") from None
        read_only = _field(parsed, "read_only", "readOnly")
        writable = _field(parsed, "writable", "can_write", "canWrite", "is_writable", "isWritable")
        mode = _field(parsed, "mode", "access", "permission", "role")
        write_key = _field(parsed, "write_token", "writeToken", "write_key", "writeKey", "write_secret", "writeSecret")
        if (
            read_only is True
            or writable is False
            or (isinstance(mode, str) and mode.lower() in {"view", "read", "readonly", "read-only"})
        ):
            raise CollabStoreValidationError("Collab link must grant write access")
        if writable is None and write_key is None and mode is None and read_only is None:
            raise CollabStoreValidationError("Collab parser did not identify write access")
        relay = _field(parsed, "ws_url", "wsUrl", "relay", "relay_url", "relayUrl", "server", "server_url", "serverUrl")
        room = _field(parsed, "room", "room_id", "roomId")
        self._validate_descriptor(relay, "relay")
        self._validate_descriptor(room, "room")
        return relay, room

    @staticmethod
    def _has_unsafe_unicode(value: str) -> bool:
        return any(unicodedata.category(character).startswith("C") for character in value)

    @staticmethod
    def _validate_descriptor(value: Any, name: str) -> None:
        if (
            not isinstance(value, str)
            or not value
            or len(value) > MAX_DESCRIPTOR_LENGTH
            or CollabStore._has_unsafe_unicode(value)
        ):
            raise CollabStoreValidationError(f"invalid {name} descriptor")

    @staticmethod
    def _validate_timestamp(value: Any, name: str) -> None:
        if not isinstance(value, str) or not value or len(value) > 64 or CollabStore._has_unsafe_unicode(value):
            raise CollabStoreValidationError(f"invalid {name}")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise CollabStoreValidationError(f"invalid {name}") from None
        if parsed.tzinfo is None:
            raise CollabStoreValidationError(f"invalid {name}")

    def _timestamp(self) -> str:
        value = self._clock()
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=UTC)
            return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
        self._validate_timestamp(value, "clock timestamp")
        return value

    def _quarantine(self) -> Path:
        stamp = re.sub(r"[^0-9]", "", self._timestamp()) or "unknown"
        return self._storage.quarantine(f"{self.path.name}.corrupt.{stamp}")

    def _write(self, data: dict[str, Any]) -> None:
        self._validate_store(data)
        try:
            payload = json.dumps(
                data,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeError, RecursionError):
            raise CollabStoreValidationError("credential store could not be serialized") from None
        if len(payload) > MAX_FILE_BYTES:
            raise CollabStoreValidationError("credential store file is too large")
        self._storage.replace(payload)
