from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ._private_state import PrivateStateFile, validate_private_directory, validate_private_file
from .attachments import MAX_IMAGE_REQUEST_BYTES, MAX_IMAGES, validated_image_content

SCHEMA_VERSION = 1
MAX_RECORDS = 1_000
MAX_STORAGE_BYTES = 128 * 1024 * 1024
MAX_TEXT_LENGTH = 4_000
RETENTION_MS = 7 * 24 * 60 * 60 * 1_000

_MAX_METADATA_BYTES = 32 * 1024 * 1024
_MAX_ENTRIES = 4_096
_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_REVISION = re.compile(r"[A-Za-z0-9_-]{16,128}")
_PAYLOAD_NAME = re.compile(r"([a-f0-9]{64})\.json")
_PAYLOAD_TEMPORARY = re.compile(r"\.([a-f0-9]{64})\.json\.[0-9a-f]{32}\.tmp")
_MANIFEST_TEMPORARY = re.compile(r"\.manifest\.json\.[0-9a-f]{32}\.tmp")
_IMAGES_UNCHANGED = object()


class DraftStoreError(RuntimeError):
    """Base class for unavailable or invalid durable draft state."""


class DraftCorruptionError(DraftStoreError):
    pass


class DraftCapacityError(DraftStoreError):
    pass


class DraftConflictError(ValueError):
    def __init__(self, draft: Mapping[str, Any]) -> None:
        super().__init__("draft revision conflict")
        self.draft = copy.deepcopy(dict(draft))


class DraftStore:
    """Crash-safe, bounded per-session draft sidecars with manifest-owned CAS."""

    def __init__(
        self,
        state_root: str | os.PathLike[str],
        *,
        clock: Callable[[], float] = time.time,
        revision_factory: Callable[[], str] = lambda: secrets.token_urlsafe(24),
        max_records: int = MAX_RECORDS,
        max_storage_bytes: int = MAX_STORAGE_BYTES,
    ) -> None:
        if not isinstance(max_records, int) or isinstance(max_records, bool) or max_records < 1:
            raise ValueError("max_records must be a positive integer")
        if not isinstance(max_storage_bytes, int) or isinstance(max_storage_bytes, bool) or max_storage_bytes < 1:
            raise ValueError("max_storage_bytes must be a positive integer")
        self.state_root = Path(state_root)
        self.root = self.state_root / "drafts"
        self.path = self.root / "manifest.json"
        self.max_records = max_records
        self.max_storage_bytes = max_storage_bytes
        self._clock = clock
        self._revision_factory = revision_factory
        self._thread_lock = threading.RLock()
        self._storage = PrivateStateFile(self.path, lock_name=".store.lock")

    def get(self, session_id: str) -> dict[str, Any]:
        self._validate_session_id(session_id)
        self._validate_state_root()
        if not self.root.exists() and not self.root.is_symlink():
            return self._public(None)
        with self._locked():
            data, inventory = self._read()
            inventory = self._prune_and_sweep(data, inventory)
            record = self._find(data, session_id)
            return self._public(record)

    def save(
        self,
        session_id: str,
        *,
        revision: str | None,
        enabled: bool,
        text: str,
        images: Any = _IMAGES_UNCHANGED,
    ) -> dict[str, Any]:
        self._validate_session_id(session_id)
        self._validate_state_root()
        revision = self._validate_expected_revision(revision)
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be boolean")
        if not isinstance(text, str) or len(text) > MAX_TEXT_LENGTH:
            raise ValueError(f"text must be a string of at most {MAX_TEXT_LENGTH} characters")
        normalized_images = _IMAGES_UNCHANGED if images is _IMAGES_UNCHANGED else self._validate_images(images)
        if not enabled and (text or (normalized_images is not _IMAGES_UNCHANGED and normalized_images)):
            raise ValueError("disabled drafts cannot contain text or images")
        payload = (
            None
            if normalized_images is _IMAGES_UNCHANGED or not normalized_images
            else self._encode_payload(normalized_images)
        )
        payload_id = hashlib.sha256(payload).hexdigest() if payload is not None else None

        with self._locked():
            data, inventory = self._read()
            inventory = self._prune_and_sweep(data, inventory)
            current = self._find(data, session_id)
            current_revision = current["revision"] if current is not None else None
            if revision != current_revision:
                raise DraftConflictError(self._public(current))
            if current is None and len(data["records"]) >= self.max_records:
                raise DraftCapacityError(f"draft storage is limited to {self.max_records} sessions")

            if enabled and normalized_images is _IMAGES_UNCHANGED and current is not None:
                payload_id = current["imagePayload"]
                payload_bytes = current["imageBytes"]
            else:
                payload_bytes = len(payload) if payload is not None else 0
            if payload is not None:
                self._put_payload(payload_id, payload, inventory)

            now_ms = max(self._now_ms(), current["updatedAt"] if current is not None else 0)
            record = {
                "sessionId": session_id,
                "revision": self._next_revision(current_revision),
                "enabled": enabled,
                "text": text if enabled else "",
                "updatedAt": now_ms,
                "expiresAt": now_ms + RETENTION_MS,
                "imagePayload": payload_id if enabled else None,
                "imageBytes": payload_bytes if enabled else 0,
            }
            if current is None:
                data["records"].append(record)
            else:
                data["records"][data["records"].index(current)] = record
            self._write(data)
            self._sweep(data, inventory)
            return self._public(record, include_images=False)

    def remove(self, session_id: str) -> bool:
        """Erase any draft state after the owning Deck session is removed."""
        self._validate_session_id(session_id)
        self._validate_state_root()
        if not self.root.exists() and not self.root.is_symlink():
            return False
        with self._locked():
            data, inventory = self._read()
            inventory = self._prune_and_sweep(data, inventory)
            record = self._find(data, session_id)
            if record is None:
                return False
            data["records"].remove(record)
            self._write(data)
            self._sweep(data, inventory)
            return True

    def _validate_state_root(self) -> None:
        try:
            validate_private_directory(self.state_root)
        except OSError as error:
            raise DraftStoreError("configured state root is unsafe") from error

    @contextmanager
    def _locked(self):
        with self._thread_lock:
            try:
                with self._storage.locked():
                    self._storage.cleanup_temporaries()
                    yield
            except OSError as error:
                raise DraftStoreError("draft storage is unavailable") from error

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {"version": SCHEMA_VERSION, "records": []}

    def _read(self) -> tuple[dict[str, Any], dict[str, Any]]:
        try:
            raw = self._storage.read(_MAX_METADATA_BYTES)
        except OSError as error:
            raise DraftStoreError("draft manifest is unavailable") from error
        if raw is None:
            data = self._empty()
        else:
            if len(raw) > _MAX_METADATA_BYTES:
                raise DraftCorruptionError("draft manifest is too large")
            try:
                data = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
                raise DraftCorruptionError("draft manifest is corrupt") from error
            self._validate_manifest(data)
        inventory = self._inventory()
        for record in data["records"]:
            payload_id = record["imagePayload"]
            if payload_id is None:
                continue
            size = inventory["payloads"].get(payload_id)
            if size is None:
                raise DraftCorruptionError("draft image payload is missing")
            if size != record["imageBytes"]:
                raise DraftCorruptionError("draft image payload does not match its manifest")
        if inventory["totalBytes"] > self.max_storage_bytes:
            raise DraftCorruptionError("draft recovery storage exceeds its capacity")
        return data, inventory

    def _validate_manifest(self, data: Any) -> None:
        if not isinstance(data, dict) or set(data) != {"version", "records"} or data.get("version") != SCHEMA_VERSION:
            raise DraftCorruptionError("draft manifest is corrupt")
        records = data.get("records")
        if not isinstance(records, list) or len(records) > self.max_records:
            raise DraftCorruptionError("draft manifest is corrupt")
        seen = set()
        for record in records:
            if not isinstance(record, dict) or set(record) != {
                "sessionId",
                "revision",
                "enabled",
                "text",
                "updatedAt",
                "expiresAt",
                "imagePayload",
                "imageBytes",
            }:
                raise DraftCorruptionError("draft manifest is corrupt")
            session_id = record["sessionId"]
            try:
                self._validate_session_id(session_id)
            except ValueError as error:
                raise DraftCorruptionError("draft manifest is corrupt") from error
            if session_id in seen:
                raise DraftCorruptionError("draft manifest contains duplicate sessions")
            seen.add(session_id)
            if not isinstance(record["revision"], str) or _REVISION.fullmatch(record["revision"]) is None:
                raise DraftCorruptionError("draft manifest is corrupt")
            if not isinstance(record["enabled"], bool):
                raise DraftCorruptionError("draft manifest is corrupt")
            if (
                not isinstance(record["text"], str)
                or len(record["text"]) > MAX_TEXT_LENGTH
                or (not record["enabled"] and record["text"])
            ):
                raise DraftCorruptionError("draft manifest is corrupt")
            for key in ("updatedAt", "expiresAt", "imageBytes"):
                if not isinstance(record[key], int) or isinstance(record[key], bool) or record[key] < 0:
                    raise DraftCorruptionError("draft manifest is corrupt")
            if record["expiresAt"] != record["updatedAt"] + RETENTION_MS:
                raise DraftCorruptionError("draft manifest is corrupt")
            payload_id = record["imagePayload"]
            payload_bytes = record["imageBytes"]
            if payload_id is None:
                if payload_bytes != 0:
                    raise DraftCorruptionError("draft manifest is corrupt")
            elif (
                not record["enabled"]
                or not isinstance(payload_id, str)
                or re.fullmatch(r"[a-f0-9]{64}", payload_id) is None
                or payload_bytes < 1
                or payload_bytes > MAX_IMAGE_REQUEST_BYTES
            ):
                raise DraftCorruptionError("draft manifest is corrupt")

    def _inventory(self) -> dict[str, Any]:
        try:
            validate_private_directory(self.root)
            entries = os.scandir(self.root)
        except OSError as error:
            raise DraftStoreError("draft storage directory is unavailable") from error
        payloads: dict[str, int] = {}
        temporaries: set[str] = set()
        total_bytes = 0
        with entries:
            for index, entry in enumerate(entries):
                if index >= _MAX_ENTRIES:
                    raise DraftCorruptionError("draft storage contains too many entries")
                path = Path(entry.path)
                name = entry.name
                try:
                    if name == "manifest.json":
                        validate_private_file(path, _MAX_METADATA_BYTES)
                    elif name in {".store.lock", ".payload.lock"}:
                        validate_private_file(path, 0)
                    elif _MANIFEST_TEMPORARY.fullmatch(name):
                        validate_private_file(path, _MAX_METADATA_BYTES)
                    elif match := _PAYLOAD_NAME.fullmatch(name):
                        validate_private_file(path, MAX_IMAGE_REQUEST_BYTES)
                        size = entry.stat(follow_symlinks=False).st_size
                        payloads[match.group(1)] = size
                        total_bytes += size
                    elif match := _PAYLOAD_TEMPORARY.fullmatch(name):
                        validate_private_file(path, MAX_IMAGE_REQUEST_BYTES)
                        temporaries.add(match.group(1))
                    else:
                        raise DraftCorruptionError("draft storage contains an unknown entry")
                except DraftCorruptionError:
                    raise
                except OSError as error:
                    raise DraftStoreError("draft storage contains an unsafe entry") from error
        return {"payloads": payloads, "temporaries": temporaries, "totalBytes": total_bytes}

    def _prune_and_sweep(self, data: dict[str, Any], inventory: dict[str, Any]) -> dict[str, Any]:
        now_ms = self._now_ms()
        retained = [record for record in data["records"] if now_ms < record["expiresAt"]]
        if len(retained) != len(data["records"]):
            data["records"] = retained
            self._write(data)
        self._sweep(data, inventory)
        return inventory

    def _sweep(self, data: Mapping[str, Any], inventory: dict[str, Any]) -> None:
        retained = {record["imagePayload"] for record in data["records"] if record["imagePayload"] is not None}
        unreferenced = set(inventory["payloads"]) - retained
        candidates = unreferenced | set(inventory["temporaries"])
        for payload_id in candidates:
            storage = self._payload_storage(payload_id)
            try:
                with storage.locked():
                    storage.cleanup_temporaries()
                    if payload_id in unreferenced:
                        storage.delete()
            except OSError as error:
                raise DraftStoreError("draft payload cleanup failed") from error
            inventory["temporaries"].discard(payload_id)
            if payload_id in unreferenced:
                removed = inventory["payloads"].pop(payload_id)
                inventory["totalBytes"] -= removed

    def _put_payload(self, payload_id: str, payload: bytes, inventory: dict[str, Any]) -> None:
        missing = payload_id not in inventory["payloads"]
        if missing and inventory["totalBytes"] + len(payload) > self.max_storage_bytes:
            raise DraftCapacityError("draft recovery storage capacity is full")
        storage = self._payload_storage(payload_id)
        try:
            with storage.locked():
                storage.cleanup_temporaries()
                current = storage.read(MAX_IMAGE_REQUEST_BYTES)
                if current is not None:
                    if current != payload:
                        raise DraftCorruptionError("draft payload conflicts with its content identity")
                else:
                    storage.replace(payload)
        except DraftCorruptionError:
            raise
        except OSError as error:
            raise DraftStoreError("draft payload cannot be persisted") from error
        if missing:
            inventory["payloads"][payload_id] = len(payload)
            inventory["totalBytes"] += len(payload)

    def _read_payload(self, record: Mapping[str, Any]) -> list[dict[str, str]]:
        payload_id = record["imagePayload"]
        storage = self._payload_storage(payload_id)
        try:
            with storage.locked():
                storage.cleanup_temporaries()
                raw = storage.read(MAX_IMAGE_REQUEST_BYTES)
        except OSError as error:
            raise DraftStoreError("draft image payload is unavailable") from error
        if raw is None or len(raw) != record["imageBytes"] or hashlib.sha256(raw).hexdigest() != payload_id:
            raise DraftCorruptionError("draft image payload is corrupt")
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
            raise DraftCorruptionError("draft image payload is corrupt") from error
        if not isinstance(value, dict) or set(value) != {"images"}:
            raise DraftCorruptionError("draft image payload is corrupt")
        try:
            return self._validate_images(value["images"])
        except ValueError as error:
            raise DraftCorruptionError("draft image payload is corrupt") from error

    def _public(self, record: Mapping[str, Any] | None, *, include_images: bool = True) -> dict[str, Any]:
        if record is None:
            return {
                "enabled": False,
                "revision": None,
                "text": "",
                "images": [],
                "updatedAt": None,
                "expiresAt": None,
            }
        result = {
            "enabled": record["enabled"],
            "revision": record["revision"],
            "text": record["text"],
            "updatedAt": self._timestamp(record["updatedAt"]),
            "expiresAt": self._timestamp(record["expiresAt"]),
        }
        if include_images:
            result["images"] = self._read_payload(record) if record["imagePayload"] is not None else []
        return result

    def _write(self, data: Mapping[str, Any]) -> None:
        encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
        if len(encoded) > _MAX_METADATA_BYTES:
            raise DraftCapacityError("draft metadata capacity is full")
        try:
            self._storage.replace(encoded)
        except OSError as error:
            raise DraftStoreError("draft manifest cannot be persisted") from error

    @staticmethod
    def _find(data: Mapping[str, Any], session_id: str) -> dict[str, Any] | None:
        return next((record for record in data["records"] if record["sessionId"] == session_id), None)

    @staticmethod
    def _validate_session_id(session_id: Any) -> None:
        if not isinstance(session_id, str) or _SESSION_ID.fullmatch(session_id) is None:
            raise ValueError("invalid session")

    @staticmethod
    def _validate_expected_revision(revision: Any) -> str | None:
        if revision is None:
            return None
        if not isinstance(revision, str) or _REVISION.fullmatch(revision) is None:
            raise ValueError("invalid draft revision")
        return revision

    @staticmethod
    def _validate_images(images: Any) -> list[dict[str, str]]:
        if not isinstance(images, list) or len(images) > MAX_IMAGES:
            raise ValueError("invalid images")
        content = []
        names: list[str | None] = []
        for image in images:
            if not isinstance(image, dict) or set(image) not in (
                {"data", "mimeType"},
                {"data", "mimeType", "name"},
            ):
                raise ValueError("invalid image")
            name = image.get("name")
            if name is not None and (
                not isinstance(name, str)
                or not name
                or len(name) > 255
                or any(ord(character) < 32 for character in name)
            ):
                raise ValueError("invalid image name")
            names.append(name)
            content.append({"type": "image", "data": image.get("data"), "mimeType": image.get("mimeType")})
        normalized = validated_image_content(content)
        result = []
        for image, name in zip(normalized, names, strict=True):
            public = {"data": image["data"], "mimeType": image["mimeType"]}
            if name is not None:
                public["name"] = name
            result.append(public)
        return result

    @staticmethod
    def _encode_payload(images: list[dict[str, str]]) -> bytes:
        encoded = json.dumps(
            {"images": images},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(encoded) > MAX_IMAGE_REQUEST_BYTES:
            raise ValueError("draft images exceed the request size limit")
        return encoded

    def _payload_storage(self, payload_id: str) -> PrivateStateFile:
        if not isinstance(payload_id, str) or re.fullmatch(r"[a-f0-9]{64}", payload_id) is None:
            raise DraftCorruptionError("invalid draft payload identity")
        return PrivateStateFile(self.root / f"{payload_id}.json", lock_name=".payload.lock")

    def _next_revision(self, previous: str | None) -> str:
        for _ in range(16):
            revision = self._revision_factory()
            if isinstance(revision, str) and _REVISION.fullmatch(revision) is not None and revision != previous:
                return revision
        raise DraftStoreError("could not allocate a draft revision")

    def _now_ms(self) -> int:
        return max(0, int(self._clock() * 1_000))

    @staticmethod
    def _timestamp(milliseconds: int) -> str:
        return (
            datetime.fromtimestamp(milliseconds / 1_000, tz=UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
