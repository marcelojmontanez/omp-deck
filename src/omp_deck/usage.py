import json
import subprocess
import threading
import time
from typing import Any


class UsageService:
    """Short-lived cache around OMP's documented structured usage command."""

    def __init__(self, omp_bin: str, *, ttl: float = 60.0) -> None:
        self.omp_bin = omp_bin
        self.ttl = ttl
        self._condition = threading.Condition()
        self._cached_at = 0.0
        self._cached: dict[str, Any] | None = None
        self._refreshing = False

    def snapshot(self) -> dict[str, Any]:
        deadline = time.monotonic() + 16
        with self._condition:
            while True:
                now = time.monotonic()
                if self._cached is not None and now - self._cached_at < self.ttl:
                    return dict(self._cached)
                if not self._refreshing:
                    self._refreshing = True
                    break
                remaining = deadline - now
                if remaining <= 0:
                    return {"reports": [], "error": "Usage is temporarily unavailable"}
                self._condition.wait(remaining)
        try:
            value = self._load()
            with self._condition:
                self._cached_at = time.monotonic()
                self._cached = dict(value)
            return value
        finally:
            with self._condition:
                self._refreshing = False
                self._condition.notify_all()

    def _load(self):
        try:
            completed = subprocess.run(
                [self.omp_bin, "usage", "--json"],
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
            if completed.returncode:
                raise RuntimeError("OMP usage command failed")
            payload = json.loads(completed.stdout)
            if not isinstance(payload, dict) or not isinstance(payload.get("reports", []), list):
                raise ValueError("invalid OMP usage response")
            return payload
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired):
            return {"reports": [], "error": "Usage is temporarily unavailable"}
