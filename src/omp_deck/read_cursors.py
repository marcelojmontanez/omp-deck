import fcntl
import json
import os
import stat
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from ._atomic_file import replace_bytes


_MAX_BYTES = 1024 * 1024


class CursorStore:
    """Crash-safe last-read message cursors keyed by stable Deck session ID."""

    def __init__(self, path):
        self.path = Path(path)
        self.lock_path = self.path.with_name(f".{self.path.name}.lock")
        self._lock = threading.RLock()
        self.quarantined_path = None
        self._load_error = None
        self._data = self._empty()
        try:
            with self._locked() as directory_fd:
                self._data = self._read(directory_fd)
        except OSError as error:
            self._load_error = str(error)

    @property
    def healthy(self):
        return self.quarantined_path is None and self._load_error is None

    def last_read(self, session_id):
        with self._lock:
            try:
                with self._locked() as directory_fd:
                    self._data = self._read(directory_fd)
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
        with self._locked() as directory_fd:
            data = self._read(directory_fd)
            data["sessions"][session_id] = {
                "messageId": message_id,
                "updatedAt": datetime.now(timezone.utc).isoformat(),
            }
            self._write(directory_fd, data)
            self._data = data
            self._load_error = None

    def remove(self, session_id):
        with self._locked() as directory_fd:
            data = self._read(directory_fd)
            if data["sessions"].pop(session_id, None) is not None:
                self._write(directory_fd, data)
            self._data = data
            self._load_error = None

    @staticmethod
    def _empty():
        return {"version": 1, "sessions": {}}

    @contextmanager
    def _locked(self):
        with self._lock:
            self._ensure_root()
            directory_fd = os.open(
                self.path.parent,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                lock_fd = os.open(
                    self.lock_path.name,
                    os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=directory_fd,
                )
                try:
                    if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
                        raise OSError("cursor lock is not a regular file")
                    os.fchmod(lock_fd, 0o600)
                    fcntl.flock(lock_fd, fcntl.LOCK_EX)
                    yield directory_fd
                finally:
                    os.close(lock_fd)
            finally:
                os.close(directory_fd)

    def _ensure_root(self):
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
            raise OSError("cursor root is unsafe")
        self.path.parent.chmod(0o700)

    def _read(self, directory_fd):
        try:
            descriptor = os.open(
                self.path.name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_fd,
            )
        except FileNotFoundError:
            return self._empty()
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_BYTES:
                raise ValueError("invalid cursor store")
            raw = os.read(descriptor, _MAX_BYTES + 1)
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
        try:
            data = json.loads(raw)
            self._validate(data)
            return data
        except (UnicodeError, json.JSONDecodeError, ValueError):
            self.quarantined_path = self._quarantine(directory_fd)
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

    def _quarantine(self, directory_fd):
        suffix = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        quarantine_name = f"{self.path.name}.corrupt-{suffix}"
        os.replace(
            self.path.name,
            quarantine_name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
        return self.path.parent / quarantine_name

    def _write(self, directory_fd, data):
        payload = json.dumps(
            data,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        if len(payload) > _MAX_BYTES:
            raise ValueError("cursor store is too large")
        replace_bytes(directory_fd, self.path.name, payload)
