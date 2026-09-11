"""Atomic adoption boundary for extension event streams."""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from typing import Any


class ExtensionEventActivation:
    """Stages callbacks until ownership and retained history are adopted."""

    def __init__(self, apply: Callable[[Mapping[str, Any]], None], *, limit: int = 512) -> None:
        self._apply = apply
        self._limit = limit
        self._lock = threading.Lock()
        self._pending: deque[Mapping[str, Any]] = deque()
        self._active = False
        self._last_sequence = 0

    def publish(self, event: Mapping[str, Any]) -> None:
        if event.get("kind") == "snapshot":
            return
        with self._lock:
            if not self._active:
                if len(self._pending) >= self._limit:
                    raise RuntimeError("extension activation event limit exceeded")
                self._pending.append(event)
                return
            self._publish_locked(event)

    def activate(self, adopt: Callable[[], None], retained: Callable[[], Iterable[Mapping[str, Any]]]) -> None:
        """Adopt ownership, replay retained events, then enable live delivery atomically."""
        with self._lock:
            adopt()
            events = [event for event in retained() if event.get("kind") != "snapshot"]
            events.extend(self._pending)
            self._pending.clear()
            for event in sorted(events, key=lambda item: item["sequence"]):
                self._publish_locked(event)
            self._active = True

    def _publish_locked(self, event: Mapping[str, Any]) -> None:
        sequence = event.get("sequence")
        if not isinstance(sequence, int) or isinstance(sequence, bool):
            raise RuntimeError("extension event has invalid sequence")
        if sequence <= self._last_sequence:
            return
        self._apply(event)
        self._last_sequence = sequence
