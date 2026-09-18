from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable

from ._rpc_errors import AdapterDisconnectedError, AdapterError

_SNAPSHOT_BUSY_RETRIES = 1
_SNAPSHOT_BUSY_DELAY_SECONDS = 0.05


class RpcRefreshScheduler:
    """Coalesces refreshes per session while isolating transports."""

    def __init__(
        self,
        refresh: Callable[[str], object],
        disconnected: Callable[[str, Exception], None],
        failed: Callable[[str, str], None],
    ) -> None:
        self._refresh = refresh
        self._disconnected = disconnected
        self._failed = failed
        self._lock = threading.Lock()
        self._running: set[str] = set()
        self._pending: set[str] = set()
        self._closed = False
        self._threads: dict[str, threading.Thread] = {}

    def refresh_now(self, session_ids: Iterable[str]) -> None:
        for session_id in session_ids:
            self._refresh_one(session_id)

    def request(self, session_ids: Iterable[str]) -> None:
        for session_id in session_ids:
            self._schedule(session_id)

    def close(self, timeout: float = 5.0) -> None:
        if timeout < 0:
            raise ValueError("timeout must not be negative")
        with self._lock:
            self._closed = True
            self._pending.clear()
            threads = tuple(self._threads.values())
        deadline = time.monotonic() + timeout
        current = threading.current_thread()
        for thread in threads:
            if thread is not current:
                thread.join(max(0.0, deadline - time.monotonic()))
        if any(thread is not current and thread.is_alive() for thread in threads):
            raise RuntimeError("RPC refresh workers did not stop")

    def _schedule(self, session_id: str) -> None:
        with self._lock:
            if self._closed:
                return
            self._pending.add(session_id)
            if session_id in self._running:
                return
            self._running.add(session_id)
            thread = threading.Thread(
                target=self._worker,
                args=(session_id,),
                name=f"omp-deck-rpc-refresh-{session_id[:16]}",
                daemon=True,
            )
            self._threads[session_id] = thread
            thread.start()

    def _worker(self, session_id: str) -> None:
        restart = False
        try:
            while True:
                with self._lock:
                    if self._closed:
                        self._pending.discard(session_id)
                        return
                    self._pending.discard(session_id)
                self._refresh_one(session_id)
                with self._lock:
                    if self._closed or session_id not in self._pending:
                        return
        finally:
            with self._lock:
                self._running.discard(session_id)
                if self._threads.get(session_id) is threading.current_thread():
                    self._threads.pop(session_id, None)
                restart = not self._closed and session_id in self._pending
            if restart:
                self._schedule(session_id)

    def _refresh_one(self, session_id: str) -> None:
        for attempt in range(_SNAPSHOT_BUSY_RETRIES + 1):
            try:
                self._refresh(session_id)
                return
            except AdapterDisconnectedError as error:
                try:
                    self._disconnected(session_id, error)
                except Exception:
                    self._report_failure(session_id, "reconnect failed")
                return
            except AdapterError as error:
                if error.kind == "snapshot_busy":
                    if attempt < _SNAPSHOT_BUSY_RETRIES:
                        time.sleep(_SNAPSHOT_BUSY_DELAY_SECONDS * (attempt + 1))
                    continue
                self._report_failure(session_id, f"refresh failed: {error.kind}")
                return
            except Exception:
                self._report_failure(session_id, "refresh failed")
                return

    def _report_failure(self, session_id: str, reason: str) -> None:
        try:
            self._failed(session_id, reason)
        except Exception:
            pass
