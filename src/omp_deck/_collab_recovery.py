from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable, Mapping
from typing import Any

_MAX_WORKERS = 4


class CollabRecoverySupervisor:
    """Runs bounded per-session recovery without delaying runtime readiness."""

    def __init__(
        self,
        stop: threading.Event,
        credentials: Any,
        model: Any,
        connect: Callable[[Mapping[str, Any], str], None],
    ) -> None:
        self._stop = stop
        self._credentials = credentials
        self._model = model
        self._connect = connect
        self._lock = threading.Lock()
        self._queue: deque[dict[str, Any]] = deque()
        self._workers: set[threading.Thread] = set()
        self._started = False

    def start(self, records: list[dict[str, Any]]) -> None:
        with self._lock:
            if self._started:
                return
            if self._stop.is_set():
                raise RuntimeError("terminal session manager is closed")
            self._started = True
            self._queue.extend(records)
            for index in range(min(_MAX_WORKERS, len(records))):
                worker = threading.Thread(
                    target=self._worker,
                    name=f"omp-deck-collab-recovery-{index + 1}",
                    daemon=True,
                )
                self._workers.add(worker)
                worker.start()

    def stop(self) -> tuple[threading.Thread, ...]:
        with self._lock:
            self._queue.clear()
            return tuple(self._workers)

    def _worker(self) -> None:
        current = threading.current_thread()
        try:
            while True:
                with self._lock:
                    if self._stop.is_set() or not self._queue:
                        return
                    record = self._queue.popleft()
                self._recover(record)
        finally:
            with self._lock:
                self._workers.discard(current)

    def _recover(self, record: Mapping[str, Any]) -> None:
        try:
            link = self._credentials.get_link(str(record["id"]))
            if link is None:
                self._disconnected(record, "Collab credential removed")
                return
            self._connect(record, link)
        except Exception:
            if self._stop.is_set():
                return
            self._disconnected(record, "Collaboration connection unavailable")

    def _disconnected(self, record: Mapping[str, Any], reason: str) -> None:
        try:
            self._model.set_transport(
                str(record["id"]),
                instance_id=None,
                generation=int(record["generation"]),
                status="disconnected",
                reason=reason,
            )
        except KeyError:
            # Removal wins over a queued recovery or a late startup failure.
            pass
