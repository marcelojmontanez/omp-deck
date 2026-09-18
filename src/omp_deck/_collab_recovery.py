from __future__ import annotations

import heapq
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

from ._rpc_errors import AdapterDisconnectedError, AdapterTimeoutError

_MAX_WORKERS = 4
_INITIAL_RETRY_DELAY = 1.0
_MAX_RETRY_DELAY = 30.0


class CollabRecoverySupervisor:
    """Runs bounded per-session recovery without delaying runtime readiness."""

    def __init__(
        self,
        stop: threading.Event,
        credentials: Any,
        model: Any,
        connect: Callable[[Mapping[str, Any], str], None],
        *,
        retry_initial: float = _INITIAL_RETRY_DELAY,
        retry_max: float = _MAX_RETRY_DELAY,
    ) -> None:
        if retry_initial <= 0 or retry_max < retry_initial:
            raise ValueError("invalid Collab recovery retry bounds")
        self._stop = stop
        self._credentials = credentials
        self._model = model
        self._connect = connect
        self._condition = threading.Condition()
        self._queue: list[tuple[float, int, dict[str, Any], float]] = []
        self._workers: set[threading.Thread] = set()
        self._started = False
        self._serial = 0
        self._retry_initial = retry_initial
        self._retry_max = retry_max

    def start(self, records: list[dict[str, Any]]) -> None:
        with self._condition:
            if self._started:
                return
            if self._stop.is_set():
                raise RuntimeError("terminal session manager is closed")
            self._started = True
            now = time.monotonic()
            for record in records:
                self._push(now, dict(record), self._retry_initial)
            for index in range(min(_MAX_WORKERS, len(records))):
                worker = threading.Thread(
                    target=self._worker,
                    name=f"omp-deck-collab-recovery-{index + 1}",
                    daemon=True,
                )
                self._workers.add(worker)
                worker.start()

    def stop(self) -> tuple[threading.Thread, ...]:
        with self._condition:
            self._queue.clear()
            self._condition.notify_all()
            return tuple(self._workers)

    def _worker(self) -> None:
        current = threading.current_thread()
        try:
            while True:
                with self._condition:
                    while True:
                        if self._stop.is_set() or not self._queue:
                            return
                        ready_at, _, record, delay = self._queue[0]
                        remaining = ready_at - time.monotonic()
                        if remaining <= 0:
                            heapq.heappop(self._queue)
                            break
                        self._condition.wait(remaining)
                if not self._recover(record):
                    continue
                with self._condition:
                    if self._stop.is_set():
                        return
                    self._push(
                        time.monotonic() + delay,
                        record,
                        min(self._retry_max, delay * 2),
                    )
                    self._condition.notify_all()
        finally:
            with self._condition:
                self._workers.discard(current)

    def _push(self, ready_at: float, record: dict[str, Any], delay: float) -> None:
        self._serial += 1
        heapq.heappush(self._queue, (ready_at, self._serial, record, delay))

    def _recover(self, record: Mapping[str, Any]) -> bool:
        try:
            link = self._credentials.get_link(str(record["id"]))
            if link is None:
                self._disconnected(record, "Collab credential removed")
                return False
            self._connect(record, link)
            return False
        except Exception as error:
            if self._stop.is_set():
                return False
            retained = self._disconnected(record, "Collaboration connection unavailable")
            return retained and _retryable(error)

    def _disconnected(self, record: Mapping[str, Any], reason: str) -> bool:
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
            return False
        return True


def _retryable(error: BaseException) -> bool:
    if isinstance(error, (OSError, AdapterTimeoutError)):
        return True
    return isinstance(error, AdapterDisconnectedError) and error.kind == "disconnected"
