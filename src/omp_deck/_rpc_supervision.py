from __future__ import annotations

import threading
from collections.abc import Callable

from ._rpc_errors import AdapterError
from .rpc_adapter import SessionAdapter


class RpcLifecycleSupervisor:
    """Owns passive observation of one SDK reader and managed child."""

    def __init__(
        self,
        adapter: SessionAdapter,
        failed: Callable[[AdapterError], None],
        *,
        label: str,
        interval: float = 0.25,
    ) -> None:
        self._adapter = adapter
        self._failed = failed
        self._interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"omp-deck-rpc-supervisor-{label[:16]}",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def request_stop(self) -> None:
        self._stop.set()

    def join(self, timeout: float) -> None:
        if self._thread is threading.current_thread():
            return
        self._thread.join(timeout)
        if self._thread.is_alive():
            raise RuntimeError("RPC lifecycle supervisor did not stop")

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._adapter.check_lifecycle(self._interval)
            except AdapterError as error:
                if not self._stop.is_set():
                    self._failed(error)
                return
            if self._stop.wait(0.1):
                return
