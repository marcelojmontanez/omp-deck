import json
import os
import stat
import threading
from datetime import datetime, timezone
from pathlib import Path


_MAX_BYTES = 1024 * 1024


class CursorStore:
    """Crash-safe last-read message cursors keyed by stable Deck session ID."""

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self.quarantined_path = None
        self._data = self._load()

    @property
    def healthy(self):
        return self.quarantined_path is None

    def last_read(self, session_id):
        with self._lock:
            value = self._data["sessions"].get(session_id)
            return value.get("messageId") if isinstance(value, dict) else None

    def mark_read(self, session_id, message_id):
        if not isinstance(session_id, str) or not session_id or len(session_id) > 128:
            raise ValueError("invalid cursor session ID")
        if not isinstance(message_id, str) or not message_id or len(message_id) > 512:
            raise ValueError("invalid cursor message ID")
        with self._lock:
            self._data["sessions"][session_id] = {
                "messageId": message_id,
                "updatedAt": datetime.now(timezone.utc).isoformat(),
            }
            self._write_locked()

    def remove(self, session_id):
        with self._lock:
            if self._data["sessions"].pop(session_id, None) is not None:
                self._write_locked()

    def _load(self):
        try:
            descriptor = os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            return {"version": 1, "sessions": {}}
        except OSError:
            self.quarantined_path = self._quarantine()
            return {"version": 1, "sessions": {}}
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_BYTES:
                raise ValueError("invalid cursor store")
            with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
                descriptor = -1
                data = json.load(stream)
            self._validate(data)
            return data
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
            self.quarantined_path = self._quarantine()
            return {"version": 1, "sessions": {}}
        finally:
            if descriptor >= 0:
                os.close(descriptor)

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
        suffix = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        quarantine = self.path.with_name(f"{self.path.name}.corrupt-{suffix}")
        os.replace(self.path, quarantine)
        self._fsync_parent()
        return quarantine

    def _write_locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        descriptor = -1
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                descriptor = -1
                json.dump(self._data, stream, separators=(",", ":"), ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
            self._fsync_parent()
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)

    def _fsync_parent(self):
        descriptor = os.open(
            self.path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
