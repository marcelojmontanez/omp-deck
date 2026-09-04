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
        self._lock = threading.Lock()
        self._cached_at = 0.0
        self._cached: dict[str, Any] | None = None

    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            if self._cached is not None and now - self._cached_at < self.ttl:
                return dict(self._cached)
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
            value = payload
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired):
            value = {"reports": [], "error": "Usage is temporarily unavailable"}
        with self._lock:
            self._cached_at = time.monotonic()
            self._cached = dict(value)
        return value
