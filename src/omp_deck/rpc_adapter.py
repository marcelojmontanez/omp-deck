from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from omp_rpc import (
    ExtensionError,
    ExtensionUiRequest,
    ReadyEvent,
    RpcCommandError,
    RpcConcurrencyError,
    RpcError,
    RpcProcessExitError,
    RpcProtocolError,
    RpcTimeoutError,
)
from ._rpc_projection import PendingUiRequest, UiOption
from ._rpc_projection import objects as _project_objects
from ._rpc_projection import plain as _plain
from ._rpc_projection import project_ui_request


class RpcClientLike(Protocol):
    def start(self) -> Any: ...
    def stop(self) -> None: ...
    def on_ready(self, listener: Callable[[ReadyEvent], None]) -> Callable[[], None]: ...
    def on_event(self, listener: Callable[[Any], None]) -> Callable[[], None]: ...
    def on_ui_request(self, listener: Callable[[ExtensionUiRequest], None]) -> Callable[[], None]: ...
    def on_protocol_error(self, listener: Callable[[RpcProtocolError], None]) -> Callable[[], None]: ...
    def on_extension_error(self, listener: Callable[[ExtensionError], None]) -> Callable[[], None]: ...
    def get_state(self) -> Any: ...
    def get_messages(self) -> Sequence[Any]: ...
    def get_todos(self) -> Sequence[Any]: ...
    def prompt(self, message: str, *, images: Sequence[Any] | None = None, streaming_behavior: str | None = None) -> None: ...
    def follow_up(self, message: str, *, images: Sequence[Any] | None = None) -> None: ...
    def abort(self) -> None: ...
    def set_model(self, provider: str, model_id: str) -> Any: ...
    def set_thinking_level(self, level: str) -> None: ...
    def send_ui_value(self, request_id: str, value: str) -> None: ...
    def send_ui_confirmation(self, request_id: str, confirmed: bool) -> None: ...
    def cancel_ui_request(self, request_id: str, *, timed_out: bool = False) -> None: ...


TransportStatus = Literal["connected", "disconnected", "incompatible"]
UiOutcome = Literal["accepted", "cancelled", "stale"]


class AdapterError(RuntimeError):
    """Base error retaining the transport failure category."""

    def __init__(self, message: str, *, kind: str, cause: BaseException | None = None):
        super().__init__(message)
        self.kind = kind
        self.__cause__ = cause


class AdapterTimeoutError(AdapterError):
    pass


class AdapterCancelledError(AdapterError):
    pass


class AdapterDisconnectedError(AdapterError):
    pass


class AdapterIncompatibleError(AdapterError):
    pass


@dataclass(frozen=True, slots=True)
class AdapterEvent:
    sequence: int
    type: str
    payload: dict[str, Any]
    timestamp: float



@dataclass(frozen=True, slots=True)
class AdapterSnapshot:
    state: dict[str, Any]
    messages: tuple[dict[str, Any], ...]
    todos: tuple[dict[str, Any], ...]
    subagents: tuple[dict[str, Any], ...]
    sequence: int
    pending_ui_request: PendingUiRequest | None
    transport_status: TransportStatus
    reason: str | None


@dataclass(slots=True)
class _UiEntry:
    projected: PendingUiRequest
    method: str
    deadline: float | None
    settled: bool = False
    in_flight: bool = False


def _objects(values: Sequence[Any]) -> tuple[dict[str, Any], ...]:
    try:
        return _project_objects(values)
    except TypeError as exc:
        raise AdapterError("RPC snapshot item is not an object", kind="protocol") from exc


class SessionAdapter:
    """Thread-safe translation boundary around the public ``RpcClient`` API."""

    def __init__(
        self,
        client_factory: Callable[[], RpcClientLike],
        *,
        on_event: Callable[[AdapterEvent], None] | None = None,
        event_limit: int = 512,
        ui_limit: int = 128,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if event_limit < 1 or ui_limit < 1:
            raise ValueError("event_limit and ui_limit must be positive")
        self._factory = client_factory
        self._callback = on_event
        self._clock = clock
        self._lock = threading.RLock()
        self._events: deque[AdapterEvent] = deque(maxlen=event_limit)
        self._ui_limit = ui_limit
        self._client: RpcClientLike | None = None
        self._unsubscribers: list[Callable[[], None]] = []
        self._ready: ReadyEvent | None = None
        self._sequence = 0
        self._status: TransportStatus = "disconnected"
        self._reason: str | None = None
        self._ui: dict[str, _UiEntry] = {}
        self._ui_order: deque[str] = deque()

    @property
    def transport_status(self) -> TransportStatus:
        with self._lock:
            return self._status

    @property
    def reason(self) -> str | None:
        with self._lock:
            return self._reason

    def start(self) -> None:
        with self._lock:
            if self._client is not None:
                raise AdapterError("adapter is already started", kind="lifecycle")
            client = self._factory()
            self._client = client
            self._ready = None
            self._reason = None
            self._unsubscribers = [
                client.on_ready(self._ingest_ready),
                client.on_event(self._ingest_agent_event),
                client.on_ui_request(self._ingest_ui_request),
                client.on_protocol_error(self._ingest_protocol_error),
                client.on_extension_error(self._ingest_extension_error),
            ]
        try:
            client.start()
            self._check_compatibility()
        except BaseException as exc:
            self._detach_and_stop(client)
            raise self._translate(exc) from exc
        with self._lock:
            self._status = "connected"
        self._emit("transport", {"status": "connected"})

    def close(self) -> None:
        with self._lock:
            client = self._client
        if client is None:
            return
        self._detach_and_stop(client)
        with self._lock:
            self._status = "disconnected"
            self._reason = "closed"
        self._emit("transport", {"status": "disconnected", "reason": "closed"})

    def _detach_and_stop(self, client: RpcClientLike) -> None:
        with self._lock:
            unsubscribers, self._unsubscribers = self._unsubscribers, []
            if self._client is not client:
                return
            self._client = None
        for unsubscribe in unsubscribers:
            try:
                unsubscribe()
            except Exception:
                pass
        try:
            client.stop()
        except BaseException:
            with self._lock:
                if self._client is None:
                    self._client = client
            raise

    def _check_compatibility(self) -> None:
        with self._lock:
            ready = self._ready
            incompatible_reason = self._reason if self._status == "incompatible" else None
        if incompatible_reason is not None:
            raise AdapterIncompatibleError(incompatible_reason, kind="incompatible")
        if ready is None:
            raise AdapterIncompatibleError("RPC client did not publish a ready event", kind="incompatible")
        versions = ready.supported_protocol_versions
        advertised = set(versions or (() if ready.protocol_version is None else (ready.protocol_version,)))
        if not advertised:
            advertised = {1}
        if not advertised.intersection({1, 2}):
            with self._lock:
                self._status = "incompatible"
                self._reason = f"unsupported RPC protocols: {sorted(advertised)}"
            raise AdapterIncompatibleError(self._reason, kind="incompatible")

    def snapshot(self) -> AdapterSnapshot:
        client = self._require_client()
        for _ in range(3):
            with self._lock:
                sequence_before = self._sequence
            try:
                state_obj = client.get_state()
                state = _plain(state_obj)
                if not isinstance(state, dict):
                    raise AdapterError("RPC state is not an object", kind="protocol")
                messages = _objects(client.get_messages())
                todos = _objects(client.get_todos())
                get_subagents = getattr(client, "get_subagents", None)
                subagents = _objects(get_subagents()) if callable(get_subagents) else ()
                pending = self.pending_ui_request()
            except BaseException as exc:
                raise self._translate(exc) from exc
            with self._lock:
                if sequence_before == self._sequence:
                    return AdapterSnapshot(
                        state,
                        messages,
                        todos,
                        subagents,
                        self._sequence,
                        pending,
                        self._status,
                        self._reason,
                    )
        raise AdapterError("RPC state changed throughout snapshot capture", kind="snapshot_busy")

    def drain_events(self) -> tuple[AdapterEvent, ...]:
        with self._lock:
            result = tuple(self._events)
            self._events.clear()
            return result

    def send_message(self, message: str, *, images: Sequence[Any] | None = None, streaming_behavior: str | None = None) -> None:
        self._call("prompt", message, images=images, streaming_behavior=streaming_behavior)

    def follow_up(self, message: str, *, images: Sequence[Any] | None = None) -> None:
        self._call("follow_up", message, images=images)

    def interrupt(self) -> None:
        self._call("abort")

    def shutdown(self) -> None:
        self.close()

    def set_model(self, provider: str, model_id: str) -> Any:
        return self._call("set_model", provider, model_id)

    def set_thinking(self, level: str) -> None:
        self._call("set_thinking_level", level)

    def branch(self, entry_id: str) -> Any:
        return self._optional_call("branch", entry_id)

    def switch(self, session_path: str) -> Any:
        return self._optional_call("switch_session", session_path)

    def resume(self, session_path: str) -> Any:
        return self.switch(session_path)

    def compact(self, custom_instructions: str | None = None) -> Any:
        return self._optional_call("compact", custom_instructions)

    def pending_ui_request(self) -> PendingUiRequest | None:
        now = self._clock()
        expired: list[str] = []
        pending: PendingUiRequest | None = None
        with self._lock:
            for request_id in self._ui_order:
                entry = self._ui.get(request_id)
                if entry is None or entry.settled or entry.in_flight:
                    continue
                if entry.deadline is not None and now >= entry.deadline:
                    entry.settled = True
                    expired.append(request_id)
                    continue
                pending = entry.projected
                break
        for request_id in expired:
            self._cancel_official(request_id, timed_out=True)
            self._emit("ui_request_timeout", {"id": request_id})
        return pending

    def respond_ui(self, request_id: str, value: str | bool) -> UiOutcome:
        with self._lock:
            entry = self._ui.get(request_id)
            if entry is None or entry.settled or entry.in_flight:
                return "stale"
            method = entry.method
            if method == "confirm" and not isinstance(value, bool):
                raise ValueError("confirm response must be boolean")
            if method != "confirm" and not isinstance(value, str):
                raise ValueError(f"{method} response must be text")
            if entry.deadline is not None and self._clock() >= entry.deadline:
                entry.settled = True
                timed_out = True
            else:
                entry.in_flight = True
                timed_out = False
        if timed_out:
            self._cancel_official(request_id, timed_out=True)
            self._emit("ui_request_timeout", {"id": request_id})
            return "stale"
        try:
            if method == "confirm":
                self._require_client().send_ui_confirmation(request_id, value)
            else:
                self._require_client().send_ui_value(request_id, value)
        except BaseException as exc:
            with self._lock:
                if not entry.settled:
                    entry.in_flight = False
            raise self._translate(exc) from exc
        with self._lock:
            entry.in_flight = False
            entry.settled = True
        self._emit("ui_request_resolved", {"id": request_id})
        return "accepted"

    def cancel_ui(self, request_id: str, *, timed_out: bool = False) -> UiOutcome:
        with self._lock:
            entry = self._ui.get(request_id)
            if entry is None or entry.settled or entry.in_flight:
                return "stale"
            entry.in_flight = True
        try:
            self._cancel_official(request_id, timed_out=timed_out)
        except BaseException:
            with self._lock:
                if not entry.settled:
                    entry.in_flight = False
            raise
        with self._lock:
            entry.in_flight = False
            entry.settled = True
        self._emit("ui_request_cancelled", {"id": request_id, "timedOut": timed_out})
        return "cancelled"

    def _cancel_official(self, request_id: str, *, timed_out: bool) -> None:
        try:
            self._require_client().cancel_ui_request(request_id, timed_out=timed_out)
        except BaseException as exc:
            raise self._translate(exc) from exc

    def _optional_call(self, method: str, *args: Any) -> Any:
        client = self._require_client()
        operation = getattr(client, method, None)
        if not callable(operation):
            raise AdapterIncompatibleError(f"RPC client does not support {method}", kind="unsupported")
        try:
            return operation(*args)
        except BaseException as exc:
            raise self._translate(exc) from exc

    def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        client = self._require_client()
        try:
            return getattr(client, method)(*args, **kwargs)
        except BaseException as exc:
            raise self._translate(exc) from exc

    def _require_client(self) -> RpcClientLike:
        with self._lock:
            if self._client is None or self._status != "connected":
                raise AdapterDisconnectedError("RPC transport is disconnected", kind="disconnected")
            return self._client

    def _ingest_ready(self, ready: ReadyEvent) -> None:
        with self._lock:
            self._ready = ready

    def _ingest_agent_event(self, event: Any) -> None:
        payload = _plain(event)
        event_type = payload.pop("type", type(event).__name__) if isinstance(payload, dict) else type(event).__name__
        self._emit(str(event_type), payload if isinstance(payload, dict) else {"value": payload})

    def _ingest_protocol_error(self, error: RpcProtocolError) -> None:
        self._emit("protocol_error", {"error": str(error)})

    def _ingest_extension_error(self, error: ExtensionError) -> None:
        payload = _plain(error)
        self._emit("extension_error", payload if isinstance(payload, dict) else {"error": str(error)})

    def _ingest_ui_request(self, request: ExtensionUiRequest) -> None:
        if request.method == "cancel":
            target = request.target_id or request.id
            with self._lock:
                entry = self._ui.get(target)
                if entry is not None:
                    entry.settled = True
            self._emit("ui_request_cancelled", {"id": target, "sourceId": request.id})
            return
        if not request.requires_response():
            self._emit("ui_request", _plain(request))
            return
        projected = project_ui_request(request, self._clock())
        deadline = projected.expires_at
        evicted: list[str] = []
        with self._lock:
            old = self._ui.get(request.id)
            if old is not None and not old.settled:
                return
            while len(self._ui_order) >= self._ui_limit:
                evicted_id = self._ui_order.popleft()
                evicted_entry = self._ui.pop(evicted_id, None)
                if evicted_entry is not None and not evicted_entry.settled:
                    evicted_entry.settled = True
                    evicted.append(evicted_id)
            self._ui[request.id] = _UiEntry(projected, request.method, deadline)
            self._ui_order.append(request.id)
        for evicted_id in evicted:
            self._cancel_official(evicted_id, timed_out=False)
            self._emit("ui_request_cancelled", {"id": evicted_id, "reason": "queue_full"})
        self._emit("ui_request", _plain(projected))

    def _emit(self, event_type: str, payload: Mapping[str, Any]) -> None:
        with self._lock:
            self._sequence += 1
            event = AdapterEvent(self._sequence, event_type, dict(payload), self._clock())
            self._events.append(event)
            callback = self._callback
        if callback is not None:
            callback(event)

    def _translate(self, exc: BaseException) -> AdapterError:
        if isinstance(exc, AdapterError):
            return exc
        if isinstance(exc, RpcTimeoutError) or isinstance(exc, TimeoutError):
            return AdapterTimeoutError(str(exc), kind="timeout", cause=exc)
        if isinstance(exc, RpcProcessExitError):
            with self._lock:
                self._status = "disconnected"
                self._reason = str(exc)
            return AdapterDisconnectedError(str(exc), kind="process_exit", cause=exc)
        if isinstance(exc, (RpcProtocolError, RpcConcurrencyError)):
            return AdapterIncompatibleError(str(exc), kind="protocol", cause=exc)
        if isinstance(exc, RpcCommandError):
            text = str(exc)
            cancellation = f"{exc.code or ''} {exc.error}".lower()
            if any(marker in cancellation for marker in ("cancel", "abort")):
                return AdapterCancelledError(text, kind="cancelled", cause=exc)
            return AdapterError(text, kind="command", cause=exc)
        if isinstance(exc, (BrokenPipeError, EOFError, ConnectionError)):
            with self._lock:
                self._status = "disconnected"
                self._reason = str(exc)
            return AdapterDisconnectedError(str(exc), kind="disconnect", cause=exc)
        if isinstance(exc, RpcError):
            return AdapterError(str(exc), kind="rpc", cause=exc)
        return AdapterError(str(exc), kind="internal", cause=exc)
