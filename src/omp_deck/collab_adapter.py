"""Synchronous, supervised guest for the COLLAB_PROTO 3 websocket protocol."""
from __future__ import annotations

import json
import copy
import random as _random
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from cryptography.exceptions import InvalidTag

from .collab_protocol import (
    COLLAB_PROTO,
    MAX_FRAME_BYTES,
    ParsedCollabLink,
    b64url_encode,
    open_frame,
    pack_envelope,
    parse_collab_link,
    seal_frame,
    unpack_envelope,
)
from .rpc_adapter import AdapterDisconnectedError, AdapterError, AdapterEvent, AdapterIncompatibleError, AdapterTimeoutError

WELCOME_TIMEOUT = 30.0
TRANSCRIPT_TIMEOUT = 10.0
_FATAL_CLOSE_CODES = frozenset({1002, 1003, 1007, 1008, 1009, 1010, 4001, 4004, 4009, 4029})
_CONNECT_OPTIONS = {
    "open_timeout": 10.0,
    "close_timeout": 10.0,
    "ping_interval": 20.0,
    "ping_timeout": 20.0,
    "max_queue": 16,
}


class _Socket(Protocol):
    def send(self, data: bytes) -> Any: ...
    def recv(self, timeout: float | None = None) -> str | bytes: ...
    def close(self) -> Any: ...


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(k): _freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v) for v in value)
    return value


@dataclass(frozen=True, slots=True)
class CollabSnapshot:
    header: Mapping[str, Any]
    state: Mapping[str, Any]
    entries: tuple[Mapping[str, Any], ...]
    agents: tuple[Mapping[str, Any], ...]
    pending_ui_requests: tuple[Mapping[str, Any], ...]
    sequence: int
    transport_status: str
    reason: str | None
    read_only: bool


@dataclass(slots=True)
class _Reply:
    event: threading.Event
    value: tuple[str, int] | BaseException | None = None


class CollabAdapter:
    """Thread-safe blocking facade over the published synchronous websocket API."""

    def __init__(
        self,
        link: str | ParsedCollabLink,
        name: str,
        *,
        on_event: Callable[[AdapterEvent], None] | None = None,
        connector: Callable[..., _Socket] | None = None,
        clock: Callable[[], float] = time.monotonic,
        random: Callable[[], float] = _random.random,
        event_limit: int = 512,
        entry_limit: int = 10000,
        ui_limit: int = 128,
        max_snapshot_bytes: int = MAX_FRAME_BYTES,
        max_frame_bytes: int = MAX_FRAME_BYTES,
    ) -> None:
        if not name or not isinstance(name, str):
            raise ValueError("name must be a non-empty string")
        if min(event_limit, entry_limit, ui_limit, max_snapshot_bytes, max_frame_bytes) < 1:
            raise ValueError("limits must be positive")
        self._snapshot_complete = False
        self._link = parse_collab_link(link) if isinstance(link, str) else link
        self._name = name
        self._callback = on_event
        if connector is None:
            from websockets.sync.client import connect
            connector = connect
        self._connector = connector
        self._clock, self._random = clock, random
        self._events: deque[AdapterEvent] = deque(maxlen=event_limit)
        self._entry_limit, self._ui_limit = entry_limit, ui_limit
        self._max_snapshot_bytes, self._max_frame_bytes = max_snapshot_bytes, max_frame_bytes
        self._lock = threading.RLock()
        self._send_lock = threading.Lock()
        self._ready = threading.Event()
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self._ws: _Socket | None = None
        self._sequence = 0
        self._status, self._reason = "disconnected", None
        self._header: dict[str, Any] = {}
        self._state: dict[str, Any] = {}
        self._entries: deque[dict[str, Any]] = deque(maxlen=entry_limit)
        self._agents: list[dict[str, Any]] = []
        self._ui: dict[int, dict[str, Any]] = {}
        self._ui_order: deque[int] = deque()
        self._host_read_only = self._link.read_only
        self._welcome = False
        self._expected_entries = 0
        self._snapshot_bytes = 0
        self._next_req = 1
        self._replies: dict[int, _Reply] = {}
        self._fatal: AdapterError | None = None

    def __repr__(self) -> str:
        return f"CollabAdapter(name={self._name!r}, read_only={self.read_only!r}, status={self.transport_status!r})"

    @property
    def read_only(self) -> bool:
        with self._lock:
            return self._link.read_only or self._host_read_only

    @property
    def transport_status(self) -> str:
        with self._lock:
            return self._status

    @property
    def reason(self) -> str | None:
        with self._lock:
            return self._reason

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                raise AdapterError("adapter is already started", kind="lifecycle")
            self._stopping.clear(); self._ready.clear(); self._fatal = None
            self._thread = threading.Thread(target=self._run, name="omp-collab-reader", daemon=False)
            self._thread.start()
        if not self._ready.wait(WELCOME_TIMEOUT):
            self.close()
            raise AdapterTimeoutError("collaboration snapshot timed out", kind="timeout")
        with self._lock:
            if self._fatal is not None:
                raise self._fatal

    def close(self) -> None:
        self._stopping.set()
        with self._lock:
            ws, thread = self._ws, self._thread
        if ws is not None:
            try: ws.close()
            except Exception: pass
        if thread is not None and thread is not threading.current_thread():
            thread.join(WELCOME_TIMEOUT)
        with self._lock:
            self._thread = None; self._ws = None
            self._fail_replies(AdapterDisconnectedError("adapter closed", kind="closed"))
            changed = self._status != "disconnected"
            self._status, self._reason = "disconnected", "closed"
        if changed: self._emit("transport", {"status": "disconnected", "reason": "closed"})

    shutdown = close

    def snapshot(self) -> CollabSnapshot:
        with self._lock:
            return CollabSnapshot(_freeze(copy.deepcopy(self._header)), _freeze(copy.deepcopy(self._state)),
                tuple(_freeze(copy.deepcopy(x)) for x in self._entries), tuple(_freeze(copy.deepcopy(x)) for x in self._agents),
                tuple(_freeze(copy.deepcopy(self._ui[i])) for i in self._ui_order if i in self._ui), self._sequence,
                self._status, self._reason, self.read_only)

    def drain_events(self) -> tuple[AdapterEvent, ...]:
        with self._lock:
            out = tuple(self._events); self._events.clear(); return out

    def send_message(self, message: str, *, images: Sequence[Mapping[str, Any]] | None = None, streaming_behavior: str | None = None) -> None:
        if streaming_behavior is not None:
            raise ValueError("streaming_behavior is not supported by collaboration")
        frame: dict[str, Any] = {"t": "prompt", "text": message}
        if images is not None:
            frame["images"] = [dict(image) for image in images]
        self._send_mutating(frame)

    prompt = send_message

    def interrupt(self) -> None: self._send_mutating({"t": "abort"})
    abort = interrupt

    def agent_chat(self, agent_id: str, text: str) -> None: self._send_mutating({"t":"agent-cmd", "cmd":"chat", "agentId":agent_id, "text":text})
    def agent_kill(self, agent_id: str) -> None: self._send_mutating({"t":"agent-cmd", "cmd":"kill", "agentId":agent_id})
    def agent_revive(self, agent_id: str) -> None: self._send_mutating({"t":"agent-cmd", "cmd":"revive", "agentId":agent_id})

    def respond_ui(self, req_id: int, value: str | None = None) -> bool:
        frame: dict[str, Any] = {"t":"ui-response", "reqId":req_id}
        if value is not None: frame["value"] = value
        data = self._encode(frame)
        self._require_writable()
        # Serialize the membership check and send: the request is removed only
        # after a successful write, while concurrent responders remain first-wins.
        with self._lock:
            if req_id not in self._ui: return False
            self._send_encoded(data)
            del self._ui[req_id]
            return True

    def fetch_transcript(self, agent_id: str, from_byte: int = 0, *, timeout: float = TRANSCRIPT_TIMEOUT) -> tuple[str, int]:
        self._require_connected()
        with self._lock:
            req_id = self._next_req
            data = self._encode({"t":"fetch-transcript", "reqId":req_id, "agentId":agent_id, "fromByte":from_byte})
            reply = _Reply(threading.Event())
            self._next_req += 1
            self._replies[req_id] = reply
        try:
            self._send_encoded(data)
            if not reply.event.wait(timeout): raise AdapterTimeoutError("transcript request timed out", kind="timeout")
            if isinstance(reply.value, BaseException): raise reply.value
            assert isinstance(reply.value, tuple)
            return reply.value
        finally:
            with self._lock: self._replies.pop(req_id, None)

    def _url(self) -> str:
        p = urlsplit(self._link.ws_url); query = dict(parse_qsl(p.query)); query["role"] = "guest"
        return urlunsplit((p.scheme, p.netloc, p.path, urlencode(query), ""))

    def _run(self) -> None:
        delay = 1.0
        while not self._stopping.is_set():
            try:
                ws = self._connector(self._url(), max_size=self._max_frame_bytes, **_CONNECT_OPTIONS)
                with self._lock:
                    self._ws = ws; self._reset_replica()
                hello: dict[str, Any] = {"t":"hello", "proto":COLLAB_PROTO, "name":self._name}
                if self._link.write_token is not None: hello["writeToken"] = b64url_encode(self._link.write_token)
                self._send(hello)
                deadline = self._clock() + WELCOME_TIMEOUT
                while not self._stopping.is_set():
                    remaining = deadline - self._clock() if not self._snapshot_complete else None
                    if remaining is not None and remaining <= 0: raise TimeoutError("snapshot timeout")
                    raw = ws.recv(timeout=remaining)
                    if self._receive(raw) and not self._snapshot_complete:
                        deadline = self._clock() + WELCOME_TIMEOUT
                    if self._snapshot_complete:
                        delay = 1.0
                return
            except BaseException as exc:
                if self._stopping.is_set(): return
                fatal = self._classify_fatal(exc)
                with self._lock:
                    self._ws = None; self._status = "disconnected"; self._reason = self._safe_reason(exc)
                    self._fail_replies(AdapterDisconnectedError("collaboration transport disconnected", kind="disconnected"))
                self._emit("transport", {"status":"disconnected", "reason":self._reason})
                if fatal:
                    with self._lock: self._fatal = fatal
                    self._ready.set(); return
                self._stopping.wait(delay * (0.75 + 0.5 * self._random()))
                delay = min(30.0, delay * 2)

    def _reset_replica(self) -> None:
        self._snapshot_complete = False; self._welcome = False; self._expected_entries = 0; self._snapshot_bytes = 0
        self._status, self._reason = "disconnected", None
        self._header = {}; self._state = {}; self._entries.clear(); self._agents = []; self._ui.clear(); self._ui_order.clear()

    def _receive(self, raw: str | bytes) -> bool:
        if isinstance(raw, str):
            if len(raw.encode("utf-8")) > self._max_frame_bytes:
                raise AdapterIncompatibleError("relay control frame exceeds maximum size", kind="bounds")
            try:
                control = json.loads(raw, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise AdapterIncompatibleError("relay sent an invalid control frame", kind="protocol") from exc
            if not isinstance(control, dict) or set(control) - {"type", "peerId", "reason"}:
                raise AdapterIncompatibleError("relay sent an unexpected control frame", kind="protocol")
            kind = control.get("type")
            peer_id = control.get("peerId")
            if kind in ("peer-joined", "peer-left") and isinstance(peer_id, int) and not isinstance(peer_id, bool) and 0 <= peer_id <= 0xFFFFFFFF and set(control) == {"type", "peerId"}:
                return False
            if kind == "room-closed" and set(control) <= {"type", "reason"} and (control.get("reason") is None or isinstance(control.get("reason"), str)):
                raise AdapterDisconnectedError("collaboration room was closed", kind="relay")
            raise AdapterIncompatibleError("relay sent an unexpected control frame", kind="protocol")
        if not isinstance(raw, bytes):
            raise AdapterIncompatibleError("relay sent an unexpected websocket frame", kind="protocol")
        peer, sealed = unpack_envelope(raw, max_frame_bytes=self._max_frame_bytes)
        frame = open_frame(self._link.key, sealed, max_frame_bytes=self._max_frame_bytes, direction="host")
        self._ingest(peer, frame)
        return frame["t"] == "snapshot-chunk"

    def _ingest(self, peer: int, frame: dict[str, Any]) -> None:
        del peer
        frame_type = frame["t"]
        with self._lock:
            if frame_type == "welcome":
                self._ingest_welcome(frame)
                return
            if not self._welcome:
                raise AdapterIncompatibleError("host frame arrived before welcome", kind="protocol")
            if frame_type == "snapshot-chunk":
                if not self._ingest_snapshot_chunk(frame):
                    return
                event_type = "transport"
                payload = {"status": "connected", "reason": None}
            else:
                if not self._snapshot_complete:
                    raise AdapterIncompatibleError("host frame arrived before snapshot completion", kind="protocol")
                event_type = frame_type.replace("-", "_")
                payload = self._ingest_live_frame(frame_type, frame)
        if payload is not None:
            self._emit(event_type, payload)

    def _ingest_welcome(self, frame: Mapping[str, Any]) -> None:
        if self._welcome or frame.get("proto") != COLLAB_PROTO:
            raise AdapterIncompatibleError("incompatible collaboration protocol", kind="incompatible")
        if any(key not in frame for key in ("header", "state", "agents", "entryCount")):
            raise AdapterIncompatibleError("malformed welcome frame", kind="protocol")
        self._welcome = True
        self._header = dict(frame["header"])
        self._state = dict(frame["state"])
        self._agents = list(frame["agents"])
        self._expected_entries = int(frame["entryCount"])
        self._host_read_only = bool(frame.get("readOnly", self._link.read_only))
        if self._expected_entries < 0 or self._expected_entries > self._entry_limit:
            raise AdapterIncompatibleError("snapshot entry limit exceeded", kind="bounds")

    def _ingest_snapshot_chunk(self, frame: Mapping[str, Any]) -> bool:
        entries = frame.get("entries")
        final = frame.get("final")
        if not isinstance(entries, list) or not isinstance(final, bool):
            raise AdapterIncompatibleError("malformed snapshot chunk", kind="protocol")
        self._snapshot_bytes += len(json.dumps(entries, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        if self._snapshot_bytes > self._max_snapshot_bytes or len(self._entries) + len(entries) > self._expected_entries:
            raise AdapterIncompatibleError("snapshot bounds exceeded", kind="bounds")
        self._entries.extend(dict(entry) for entry in entries)
        if not final:
            return False
        if len(self._entries) != self._expected_entries:
            raise AdapterIncompatibleError("incomplete collaboration snapshot", kind="protocol")
        self._snapshot_complete = True
        self._status, self._reason = "connected", None
        self._ready.set()
        return True

    def _ingest_live_frame(self, frame_type: str, frame: Mapping[str, Any]) -> Mapping[str, Any] | None:
        if frame_type == "entry":
            entry = frame["entry"]
            self._entries.append(dict(entry))
            return entry
        if frame_type == "event":
            return frame["event"]
        if frame_type == "state":
            self._state = dict(frame["state"])
            return frame["state"]
        if frame_type == "agents":
            self._agents = list(frame["agents"])
            return {"agents": frame["agents"]}
        if frame_type == "bus":
            return {"channel": frame["channel"], "data": frame.get("data")}
        if frame_type == "ui-request":
            return self._ingest_ui_request(frame)
        if frame_type == "ui-request-end":
            return self._ingest_ui_request_end(frame)
        if frame_type == "transcript":
            return self._ingest_transcript(frame)
        if frame_type in ("bye", "error"):
            reason = frame.get("reason") or frame.get("message") or "host closed session"
            raise AdapterDisconnectedError(str(reason), kind=frame_type)
        raise AdapterIncompatibleError("unsupported host frame", kind="protocol")

    def _ingest_ui_request(self, frame: Mapping[str, Any]) -> Mapping[str, Any] | None:
        request = dict(frame["request"])
        req_id = request.get("reqId")
        if not isinstance(req_id, int) or req_id in self._ui:
            return None
        while len(self._ui_order) >= self._ui_limit:
            old = self._ui_order.popleft()
            self._ui.pop(old, None)
        self._ui[req_id] = request
        self._ui_order.append(req_id)
        return request

    def _ingest_ui_request_end(self, frame: Mapping[str, Any]) -> Mapping[str, Any] | None:
        req_id = frame.get("reqId")
        if req_id not in self._ui:
            return None
        self._ui.pop(req_id, None)
        return {"reqId": req_id}

    def _ingest_transcript(self, frame: Mapping[str, Any]) -> Mapping[str, Any] | None:
        req_id = frame.get("reqId")
        reply = self._replies.get(req_id)
        if reply is None or reply.event.is_set():
            return None
        error = frame.get("error")
        if error is not None:
            reply.value = AdapterError(str(error), kind="remote")
        else:
            reply.value = (str(frame.get("text", "")), int(frame["newSize"]))
        reply.event.set()
        return {"reqId": req_id, "newSize": frame.get("newSize"), "error": error}

    def _require_connected(self) -> None:
        with self._lock:
            if self._status != "connected" or self._ws is None: raise AdapterDisconnectedError("collaboration transport is not connected", kind="disconnected")

    def _require_writable(self) -> None:
        self._require_connected()
        if self.read_only: raise AdapterError("collaboration link is read-only", kind="read_only")

    def _send_mutating(self, frame: dict[str, Any]) -> None:
        data = self._encode(frame)
        self._require_writable()
        self._send_encoded(data)

    def _encode(self, frame: dict[str, Any]) -> bytes:
        return pack_envelope(0, seal_frame(self._link.key, frame, max_frame_bytes=self._max_frame_bytes))

    def _send(self, frame: dict[str, Any]) -> None:
        self._send_encoded(self._encode(frame))

    def _send_encoded(self, data: bytes) -> None:
        with self._lock: ws = self._ws
        if ws is None: raise AdapterDisconnectedError("collaboration transport is not connected", kind="disconnected")
        with self._send_lock: ws.send(data)

    def _emit(self, event_type: str, payload: Mapping[str, Any]) -> None:
        with self._lock:
            self._sequence += 1; event = AdapterEvent(self._sequence, event_type, copy.deepcopy(dict(payload)), self._clock())
            self._events.append(event); callback = self._callback
        if callback:
            try: callback(event)
            except Exception: pass

    def _fail_replies(self, exc: BaseException) -> None:
        for reply in self._replies.values():
            if not reply.event.is_set(): reply.value = exc; reply.event.set()

    @staticmethod
    def _safe_reason(exc: BaseException) -> str:
        if isinstance(exc, (InvalidTag, ValueError)): return "invalid encrypted collaboration frame"
        return "collaboration transport disconnected"

    @staticmethod
    def _classify_fatal(exc: BaseException) -> AdapterError | None:
        if isinstance(exc, AdapterIncompatibleError): return exc
        if isinstance(exc, AdapterDisconnectedError) and exc.kind == "relay": return exc
        if isinstance(exc, (InvalidTag, ValueError)): return AdapterIncompatibleError("invalid encrypted collaboration frame", kind="crypto")
        code = getattr(exc, "code", None) or getattr(getattr(exc, "rcvd", None), "code", None)
        if code in _FATAL_CLOSE_CODES: return AdapterDisconnectedError("relay rejected collaboration connection", kind="relay")
        return None
