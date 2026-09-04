from __future__ import annotations

import threading
from collections.abc import Callable, Iterable

from .rpc_adapter import AdapterDisconnectedError, AdapterError


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

    def refresh_now(self, session_ids: Iterable[str]) -> None:
        for session_id in session_ids:
            self._refresh_one(session_id)

    def request(self, session_ids: Iterable[str]) -> None:
        for session_id in session_ids:
            self._schedule(session_id)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._pending.clear()

    def _schedule(self, session_id: str) -> None:
        with self._lock:
            if self._closed:
                return
            self._pending.add(session_id)
            if session_id in self._running:
                return
            self._running.add(session_id)
        threading.Thread(
            target=self._worker,
            args=(session_id,),
            name=f"omp-deck-rpc-refresh-{session_id[:16]}",
            daemon=True,
        ).start()

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
                restart = not self._closed and session_id in self._pending
            if restart:
                self._schedule(session_id)

    def _refresh_one(self, session_id: str) -> None:
        try:
            self._refresh(session_id)
        except AdapterDisconnectedError as error:
            try:
                self._disconnected(session_id, error)
            except Exception:
                self._report_failure(session_id, "reconnect failed")
        except AdapterError as error:
            self._report_failure(session_id, f"refresh failed: {error.kind}")
        except Exception:
            self._report_failure(session_id, "refresh failed")

    def _report_failure(self, session_id: str, reason: str) -> None:
        try:
            self._failed(session_id, reason)
        except Exception:
            pass
