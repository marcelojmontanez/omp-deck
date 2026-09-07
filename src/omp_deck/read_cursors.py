import json
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from ._private_state import PrivateStateFile

_MAX_BYTES = 1024 * 1024


class CursorStore:
    """Crash-safe last-read message cursors keyed by stable Deck session ID."""

    def __init__(self, path):
        self.path = Path(path)
        self.lock_path = self.path.with_name(f".{self.path.name}.lock")
        self._storage = PrivateStateFile(self.path)
        self._lock = threading.RLock()
        self.quarantined_path = None
        self._load_error = None
        self._data = self._empty()
        try:
            with self._locked():
                self._data = self._read()
        except OSError as error:
            self._load_error = str(error)

    @property
    def healthy(self):
        return self.quarantined_path is None and self._load_error is None

    def last_read(self, session_id):
        with self._lock:
            try:
                with self._locked():
                    self._data = self._read()
                self._load_error = None
            except OSError as error:
                self._load_error = str(error)
                return None
            value = self._data["sessions"].get(session_id)
            return value.get("messageId") if isinstance(value, dict) else None

    def mark_read(self, session_id, message_id):
        if not isinstance(session_id, str) or not session_id or len(session_id) > 128:
            raise ValueError("invalid cursor session ID")
        if not isinstance(message_id, str) or not message_id or len(message_id) > 512:
            raise ValueError("invalid cursor message ID")
        with self._locked():
            data = self._read()
            data["sessions"][session_id] = {
                "messageId": message_id,
                "updatedAt": datetime.now(UTC).isoformat(),
            }
            self._write(data)
            self._data = data
            self._load_error = None

    def remove(self, session_id):
        with self._locked():
            data = self._read()
            if data["sessions"].pop(session_id, None) is not None:
                self._write(data)
            self._data = data
            self._load_error = None

    @staticmethod
    def _empty():
        return {"version": 1, "sessions": {}}

    @contextmanager
    def _locked(self):
        with self._lock:
            with self._storage.locked():
                yield

    def _read(self):
        try:
            raw = self._storage.read(_MAX_BYTES)
            if raw is None:
                return self._empty()
            data = json.loads(raw)
            self._validate(data)
            return data
        except (UnicodeError, json.JSONDecodeError, ValueError):
            self.quarantined_path = self._quarantine()
            return self._empty()

    def _validate(self, data):
        if not isinstance(data, dict) or data.get("version") != 1:
            raise ValueError("invalid cursor store")
        sessions = data.get("sessions")
        if not isinstance(sessions, dict) or len(sessions) > 10_000:
            raise ValueError("invalid cursor store")
        for session_id, value in sessions.items():
            if (
                not isinstance(session_id, str)
                or not session_id
                or len(session_id) > 128
                or not isinstance(value, dict)
                or not isinstance(value.get("messageId"), str)
                or not value["messageId"]
                or len(value["messageId"]) > 512
            ):
                raise ValueError("invalid cursor store")

    def _quarantine(self):
        suffix = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        return self._storage.quarantine(f"{self.path.name}.corrupt-{suffix}")

    def _write(self, data):
        payload = json.dumps(
            data,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        if len(payload) > _MAX_BYTES:
            raise ValueError("cursor store is too large")
        self._storage.replace(payload)
