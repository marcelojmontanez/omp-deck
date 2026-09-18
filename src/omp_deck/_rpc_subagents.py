import threading
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from omp_rpc import UnknownNotification

from ._rpc_projection import objects

_MAX_SUBAGENTS = 128


class SubagentProtocolError(ValueError):
    """A native subagent response does not match its published shape."""


class SubagentClient(Protocol):
    def request_raw(self, command_type: str, **payload: Any) -> dict[str, Any]: ...
    def on_unknown_notification(self, listener: Callable[[UnknownNotification], None]) -> Callable[[], None]: ...


class _SubagentSubscription:
    """Own one coalesced focused-refresh worker and its retirement boundary."""

    def __init__(self, client: SubagentClient, emit: Callable[[str, Mapping[str, Any]], None]) -> None:
        self._client = client
        self._emit = emit
        self._lock = threading.Lock()
        self._closed = False
        self._pending = False
        self._worker: threading.Thread | None = None
        self._unsubscribe_native: Callable[[], None] | None = None

    def start(self) -> None:
        self._unsubscribe_native = self._client.on_unknown_notification(self._receive)
        try:
            result = self._client.request_raw("set_subagent_subscription", level="progress")
            if result.get("level") != "progress":
                raise SubagentProtocolError("subagent progress subscription was not accepted")
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._pending = False
            worker = self._worker
            unsubscribe_native = self._unsubscribe_native
            self._unsubscribe_native = None
        if unsubscribe_native is not None:
            unsubscribe_native()
        if worker is not None and worker is not threading.current_thread():
            worker.join()

    def _receive(self, notification: UnknownNotification) -> None:
        frame = notification.payload
        kind = frame.get("type")
        if kind not in {"subagent_lifecycle", "subagent_progress"}:
            return
        with self._lock:
            if self._closed:
                return
        payload = frame.get("payload")
        if not isinstance(payload, Mapping):
            self._emit("protocol_error", {"error": "invalid subagent notification"})
            return
        self._emit(kind, payload)
        self._request_refresh()

    def _request_refresh(self) -> None:
        with self._lock:
            if self._closed:
                return
            if self._worker is not None:
                self._pending = True
                return
            worker = threading.Thread(
                target=self._refresh,
                name="omp-deck-rpc-subagents",
                daemon=True,
            )
            self._worker = worker
            worker.start()

    def _refresh(self) -> None:
        restart = False
        try:
            while True:
                try:
                    subagents = snapshot_subagents(self._client)
                    if not self._deliver("subagents", {"subagents": list(subagents)}):
                        return
                except BaseException as error:
                    with self._lock:
                        self._pending = False
                    try:
                        self._deliver("subagent_refresh_failed", {"error": str(error)})
                    except BaseException:
                        pass
                    return
                with self._lock:
                    if self._closed or not self._pending:
                        return
                    self._pending = False
        finally:
            with self._lock:
                if self._worker is threading.current_thread():
                    self._worker = None
                    restart = not self._closed and self._pending
            if restart:
                self._request_refresh()

    def _deliver(self, kind: str, payload: Mapping[str, Any]) -> bool:
        with self._lock:
            if self._closed:
                return False
        self._emit(kind, payload)
        return True


def subscribe_subagents(client: SubagentClient, emit: Callable[[str, Mapping[str, Any]], None]) -> Callable[[], None]:
    subscription = _SubagentSubscription(client, emit)
    subscription.start()
    return subscription.close


def snapshot_subagents(client: SubagentClient) -> tuple[dict[str, Any], ...]:
    response = client.request_raw("get_subagents")
    subagents = response.get("subagents")
    if not isinstance(subagents, list):
        raise SubagentProtocolError("invalid subagent snapshot")
    try:
        result = objects(subagents[:_MAX_SUBAGENTS])
    except TypeError as exc:
        raise SubagentProtocolError("invalid subagent snapshot") from exc
    for item in result:
        progress = item.get("progress", {})
        if not isinstance(progress, Mapping):
            raise SubagentProtocolError("invalid subagent progress")
        item["label"] = item.get("description") or item.get("agent") or item.get("id")
        item["activitySummary"] = progress.get("lastIntent") or progress.get("currentTool") or item.get("status")
    return result
