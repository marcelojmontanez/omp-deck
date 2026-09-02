import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path


class ReadCursorStore:
    """Thread-safe, crash-safe storage for per-session event cursors."""

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def _load(self):
        if not self.path.exists():
            return {"version": 1, "sessions": {}}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if data.get("version") != 1 or not isinstance(data.get("sessions"), dict):
            raise ValueError("invalid read cursor store")
        return data

    def get(self, session_id):
        with self._lock:
            return self._load()["sessions"].get(session_id, {}).get("eventId")

    def set(self, session_id, event_id):
        with self._lock:
            data = self._load()
            self._set_unlocked(data, session_id, event_id)
            return event_id

    def advance(self, session_id, event_id, event_ids):
        with self._lock:
            data = self._load()
            existing = data["sessions"].get(session_id, {}).get("eventId")
            if existing and existing not in event_ids:
                return existing
            if existing in event_ids and event_ids.index(existing) > event_ids.index(event_id):
                event_id = existing
            self._set_unlocked(data, session_id, event_id)
            return event_id

    def _set_unlocked(self, data, session_id, event_id):
        data["sessions"][session_id] = {
            "eventId": event_id,
            "updatedAt": datetime.now(timezone.utc).isoformat(),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)
        directory_fd = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
