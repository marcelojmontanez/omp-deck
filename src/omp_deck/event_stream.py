from __future__ import annotations

import json
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping


class ReplayUnavailableError(LookupError):
    """The requested event ID is outside the bounded replay window."""


@dataclass(frozen=True, slots=True)
class FleetEvent:
    id: int
    snapshot_generation: int
    session_id: str | None
    instance_id: str | None
    generation: int | None
    sequence: int | None
    type: str
    _payload_json: str
    timestamp: str

    @property
    def payload(self) -> dict[str, Any]:
        return json.loads(self._payload_json)

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "snapshotGeneration": self.snapshot_generation,
            "sessionId": self.session_id,
            "instanceId": self.instance_id,
            "generation": self.generation,
            "sequence": self.sequence,
            "type": self.type,
            "payload": self.payload,
            "timestamp": self.timestamp,
        }


class EventJournal:
    """Thread-safe global event order with count and encoded-byte bounds."""

    def __init__(self, *, max_events: int = 2_000, max_bytes: int = 4 * 1024 * 1024) -> None:
        if max_events < 1 or max_bytes < 1:
            raise ValueError("event journal bounds must be positive")
        self._max_events = max_events
        self._max_bytes = max_bytes
        self._events: deque[tuple[FleetEvent, int]] = deque()
        self._bytes = 0
        self._next_id = 1
        self._condition = threading.Condition()

    @property
    def latest_id(self) -> int:
        with self._condition:
            return self._next_id - 1

    def publish(
        self,
        *,
        snapshot_generation: int,
        event_type: str,
        payload: Mapping[str, Any],
        session_id: str | None = None,
        instance_id: str | None = None,
        generation: int | None = None,
        sequence: int | None = None,
    ) -> FleetEvent:
        if not event_type:
            raise ValueError("event_type is required")
        if not isinstance(payload, Mapping):
            raise TypeError("event payload must be an object")
        payload_json = json.dumps(
            dict(payload), separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
        parsed_payload = json.loads(payload_json)
        if not isinstance(parsed_payload, dict):
            raise TypeError("event payload must be an object")
        payload_json = json.dumps(
            parsed_payload, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
        with self._condition:
            event = FleetEvent(
                id=self._next_id,
                snapshot_generation=snapshot_generation,
                session_id=session_id,
                instance_id=instance_id,
                generation=generation,
                sequence=sequence,
                type=event_type,
                _payload_json=payload_json,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
            encoded_size = len(
                json.dumps(
                    event.public(), separators=(",", ":"), ensure_ascii=False, allow_nan=False
                ).encode("utf-8")
            )
            self._next_id += 1
            self._events.append((event, encoded_size))
            self._bytes += encoded_size
            while len(self._events) > self._max_events or self._bytes > self._max_bytes:
                _, removed_size = self._events.popleft()
                self._bytes -= removed_size
            self._condition.notify_all()
            return event

    def replay_after(self, event_id: int) -> tuple[FleetEvent, ...]:
        if event_id < 0:
            raise ValueError("event_id cannot be negative")
        with self._condition:
            self._validate_cursor_locked(event_id)
            return tuple(event for event, _ in self._events if event.id > event_id)

    def wait_after(self, event_id: int, timeout: float) -> tuple[FleetEvent, ...]:
        if timeout < 0:
            raise ValueError("timeout cannot be negative")
        with self._condition:
            self._validate_cursor_locked(event_id)
            if self._next_id - 1 == event_id:
                self._condition.wait(timeout)
            self._validate_cursor_locked(event_id)
            return tuple(event for event, _ in self._events if event.id > event_id)

    def _validate_cursor_locked(self, event_id: int) -> None:
        latest = self._next_id - 1
        if event_id > latest:
            raise ReplayUnavailableError(event_id)
        if self._events:
            if event_id < self._events[0][0].id - 1:
                raise ReplayUnavailableError(event_id)
        elif event_id < latest:
            raise ReplayUnavailableError(event_id)
