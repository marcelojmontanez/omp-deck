"""Synchronous client for the OMP Deck fallback extension protocol."""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Any

from ._extension_protocol import (
    ExtensionAdapterError,
    ExtensionDisconnected,
    ExtensionProtocolError,
    ExtensionRegistration,
    ExtensionRequestTimeout,
    ExtensionSnapshot,
    _exact_keys,
    _immutable,
    _object,
    _Pending,
)

PROTOCOL = "omp-deck-fallback-v1"
_MISSING = object()
_CONNECTION_TOKEN = re.compile(r"[0-9a-f]{64}")


def _default_connector(path: str):
    if os.name == "nt":
        from ._windows_pipe import connect_named_pipe

        return connect_named_pipe(path)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(path)
    return sock


class ExtensionAdapter:
    def __init__(
        self,
        socket_path: str,
        on_event: Callable[[Mapping[str, Any]], None] | None = None,
        *,
        connector: Callable[[str], Any] = _default_connector,
        clock: Callable[[], float] = time.monotonic,
        frame_limit: int = 1024 * 1024,
        event_limit: int = 512,
        response_limit: int = 128,
        registration_timeout: float = 5,
        connection_token: str | None = None,
        heartbeat_timeout: float = 50,
    ) -> None:
        if not isinstance(socket_path, str) or not socket_path or "\x00" in socket_path:
            raise ValueError("socket_path must be a non-empty extension endpoint")
        if on_event is not None and not callable(on_event):
            raise TypeError("on_event must be callable")
        if not callable(connector) or not callable(clock):
            raise TypeError("connector and clock must be callable")
        for name, value in (
            ("frame_limit", frame_limit),
            ("event_limit", event_limit),
            ("response_limit", response_limit),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        for name, value in (("registration_timeout", registration_timeout), ("heartbeat_timeout", heartbeat_timeout)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f"{name} must be positive")
        if connection_token is not None and (
            not isinstance(connection_token, str) or _CONNECTION_TOKEN.fullmatch(connection_token) is None
        ):
            raise ValueError("connection_token must be a 256-bit lowercase hexadecimal token")
        self.socket_path = socket_path
        self._on_event = on_event
        self._connector = connector
        self._connection_token = connection_token
        self._clock = clock
        self._frame_limit = frame_limit
        self._event_limit = event_limit
        self._response_limit = response_limit
        self._registration_timeout = float(registration_timeout)
        self._heartbeat_timeout = float(heartbeat_timeout)
        self._sock: Any | None = None
        self._reader: threading.Thread | None = None
        self._buffer = bytearray()
        self._send_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._events: deque[Mapping[str, Any]] = deque(maxlen=event_limit)
        self._pending: dict[str, _Pending] = {}
        self._stale: deque[str] = deque(maxlen=response_limit)
        self._next_id = 0
        self._sequence = 0
        self._last_heartbeat = 0.0
        self._registration: ExtensionRegistration | None = None
        self._snapshot: ExtensionSnapshot | None = None
        self._closed = True
        self._reason: str | None = None

    @property
    def registration(self) -> ExtensionRegistration | None:
        return self._registration

    def start(self) -> ExtensionRegistration:
        with self._state_lock:
            if not self._closed:
                if self._registration is None:
                    raise ExtensionAdapterError("adapter is starting")
                return self._registration
            self._closed = False
            self._reason = None
        sock: Any | None = None
        try:
            sock = self._connector(self.socket_path)
            if not all(hasattr(sock, name) for name in ("recv", "sendall", "settimeout", "close")):
                try:
                    sock.close()
                finally:
                    sock = None
                raise TypeError("connector did not return a socket")
            self._sock = sock
            if isinstance(sock, socket.socket) and hasattr(socket, "SO_PEERCRED"):
                credentials = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                peer_uid = int.from_bytes(credentials[4:8], byteorder=os.sys.byteorder, signed=True)
                if peer_uid != os.geteuid():
                    raise ExtensionProtocolError("extension peer ownership mismatch")
            sock.settimeout(self._registration_timeout)
            if self._connection_token is not None:
                handshake = (
                    json.dumps(
                        {"protocol": PROTOCOL, "version": 1, "type": "connect", "token": self._connection_token},
                        separators=(",", ":"),
                    ).encode("utf-8")
                    + b"\n"
                )
                sock.sendall(handshake)
            registration = self._parse_registration(self._read_frame())
            snapshot_frame = self._parse_event(self._read_frame(), require_snapshot=True)
            self._registration = registration
            self._apply_event(snapshot_frame)
            self._last_heartbeat = self._clock()
            sock.settimeout(min(1.0, self._heartbeat_timeout / 4))
            reader = threading.Thread(target=self._reader_loop, name="omp-deck-extension", daemon=True)
            self._reader = reader
            reader.start()
            return registration
        except Exception:
            self._disconnect("startup failed")
            raise

    def close(self) -> None:
        self._disconnect("closed")
        reader = self._reader
        if reader is not None and reader is not threading.current_thread():
            reader.join()

    shutdown = close

    def snapshot(self) -> ExtensionSnapshot | None:
        with self._state_lock:
            return self._snapshot

    def drain_events(self) -> tuple[Mapping[str, Any], ...]:
        with self._state_lock:
            events = tuple(self._events)
            self._events.clear()
            return events

    def send_message(self, message: str, streaming_behavior: str | None = None) -> Any:
        if not isinstance(message, str) or not message or len(message.encode("utf-8")) > self._frame_limit // 2:
            raise ValueError("message must be a non-empty bounded string")
        if streaming_behavior not in (None, "steer", "followUp"):
            raise ValueError("streaming_behavior must be None, 'steer', or 'followUp'")
        return self._request("followUp" if streaming_behavior == "followUp" else "sendMessage", message=message)

    def interrupt(self) -> Any:
        return self._request("interrupt")

    def shutdown_session(self) -> Any:
        return self._request("shutdown")

    def refresh(self) -> ExtensionSnapshot:
        result = _object(self._request("snapshot"), "snapshot response")
        event_sequence = result.get("eventSequence")
        if not isinstance(event_sequence, int) or isinstance(event_sequence, bool) or event_sequence < 1:
            raise ExtensionProtocolError("invalid snapshot response sequence")
        with self._state_lock:
            if event_sequence < self._sequence:
                if self._snapshot is None:
                    raise ExtensionProtocolError("stale snapshot response")
                return self._snapshot
            if event_sequence > self._sequence:
                raise ExtensionProtocolError("future snapshot response")
            snapshot = self._snapshot_from_data(result, event_sequence)
            self._snapshot = snapshot
            return snapshot

    def sync_history(self) -> None:
        self._request("syncHistory")

    def _request(self, action: str, **fields: Any) -> Any:
        with self._state_lock:
            if self._closed or self._sock is None:
                raise ExtensionDisconnected(self._reason or "extension disconnected")
            if len(self._pending) >= self._response_limit:
                raise ExtensionAdapterError("response limit exceeded")
            self._next_id += 1
            request_id = str(self._next_id)
            pending = _Pending(threading.Event())
            self._pending[request_id] = pending
        frame = {"protocol": PROTOCOL, "type": "request", "id": request_id, "action": action, **fields}
        try:
            self._send(frame)
        except Exception:
            with self._state_lock:
                self._pending.pop(request_id, None)
            self._disconnect("write failed")
            raise ExtensionDisconnected("extension disconnected") from None
        if not pending.ready.wait(self._registration_timeout):
            with self._state_lock:
                self._pending.pop(request_id, None)
                self._stale.append(request_id)
            raise ExtensionRequestTimeout("extension request timed out")
        if pending.disconnected or pending.response is None:
            raise ExtensionDisconnected(self._reason or "extension disconnected")
        response = pending.response
        if not response["ok"]:
            raise ExtensionAdapterError("extension rejected request")
        return response.get("result")

    def _send(self, frame: Mapping[str, Any]) -> None:
        encoded = json.dumps(frame, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"
        if len(encoded) > self._frame_limit:
            raise ValueError("request frame exceeds limit")
        with self._send_lock:
            sock = self._sock
            if sock is None:
                raise OSError("closed")
            sock.sendall(encoded)

    def _read_frame(self) -> dict[str, Any]:
        while True:
            newline = self._buffer.find(b"\n")
            if newline >= 0:
                raw = bytes(self._buffer[:newline])
                del self._buffer[: newline + 1]
                if not raw:
                    continue
                try:
                    decoded = raw.decode("utf-8")
                    value = json.loads(decoded, parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()))
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                    raise ExtensionProtocolError("invalid JSON frame") from None
                return _object(value, "frame")
            # Never request enough bytes for a line to grow beyond the configured cap.
            remaining = self._frame_limit - len(self._buffer)
            if remaining <= 0:
                raise ExtensionProtocolError("frame exceeds limit")
            sock = self._sock
            if sock is None:
                raise ExtensionDisconnected("extension disconnected")
            chunk = sock.recv(min(65536, remaining))
            if not chunk:
                raise ExtensionDisconnected("extension disconnected")
            self._buffer.extend(chunk)

    def _parse_registration(self, frame: dict[str, Any]) -> ExtensionRegistration:
        required = {
            "protocol",
            "version",
            "type",
            "ompVersion",
            "compatible",
            "connectionId",
            "session",
            "sessionId",
            "title",
            "project",
            "capabilities",
        }
        _exact_keys(frame, required, set(), "registration")
        if (
            frame["protocol"] != PROTOCOL
            or frame["version"] != 1
            or frame["type"] != "registration"
            or frame["compatible"] is not True
        ):
            raise ExtensionProtocolError("incompatible registration")
        strings = (frame["ompVersion"], frame["connectionId"], frame["sessionId"], frame["project"])
        if any(not isinstance(value, str) or not value or len(value) > 4096 for value in strings):
            raise ExtensionProtocolError("invalid registration")
        if frame["title"] is not None and (not isinstance(frame["title"], str) or len(frame["title"]) > 4096):
            raise ExtensionProtocolError("invalid registration")
        session = _object(frame["session"], "session")
        _exact_keys(session, {"id", "file", "title"}, set(), "session")
        if (
            session["id"] != frame["sessionId"]
            or session["title"] != frame["title"]
            or (session["file"] is not None and not isinstance(session["file"], str))
        ):
            raise ExtensionProtocolError("invalid session identity")
        caps = _object(frame["capabilities"], "capabilities")
        expected_caps = {
            "sendMessage",
            "followUp",
            "interrupt",
            "shutdown",
            "todos",
            "subagents",
            "answerUiRequest",
            "answer_ui_request",
        }
        _exact_keys(caps, expected_caps, set(), "capabilities")
        if any(type(value) is not bool for value in caps.values()):
            raise ExtensionProtocolError("invalid capabilities")
        basename = os.path.basename(self.socket_path)
        socket_id = hashlib.sha256(frame["sessionId"].encode("utf-8")).hexdigest()[:24]
        suffix = ".pipe" if os.name == "nt" else ".sock"
        if not basename.startswith(socket_id + ".") or not basename.endswith("." + frame["connectionId"] + suffix):
            raise ExtensionProtocolError("registration socket identity mismatch")
        return ExtensionRegistration(
            1,
            frame["ompVersion"],
            frame["sessionId"],
            frame["project"],
            frame["title"],
            frame["connectionId"],
            MappingProxyType(dict(caps)),
        )

    def _parse_event(self, frame: dict[str, Any], *, require_snapshot: bool = False) -> Mapping[str, Any]:
        _exact_keys(frame, {"protocol", "type", "sequence", "kind", "data"}, set(), "event")
        if (
            frame["protocol"] != PROTOCOL
            or frame["type"] != "event"
            or type(frame["sequence"]) is not int
            or frame["sequence"] <= 0
        ):
            raise ExtensionProtocolError("invalid event envelope")
        if not isinstance(frame["kind"], str) or not frame["kind"] or len(frame["kind"]) > 128:
            raise ExtensionProtocolError("invalid event kind")
        if require_snapshot and frame["kind"] != "snapshot":
            raise ExtensionProtocolError("authoritative snapshot required")
        return MappingProxyType(
            {"sequence": frame["sequence"], "kind": frame["kind"], "data": _immutable(frame["data"])}
        )

    def _snapshot_from_data(self, data: Any, sequence: int) -> ExtensionSnapshot:
        data = _object(data, "snapshot")
        _exact_keys(data, {"idle", "pendingMessages", "model", "async"}, {"eventSequence"}, "snapshot")
        if type(data["idle"]) is not bool or type(data["pendingMessages"]) is not bool:
            raise ExtensionProtocolError("invalid snapshot")
        model = data["model"]
        frozen_model = None
        if model is not None:
            model = _object(model, "model")
            _exact_keys(model, {"provider", "id"}, set(), "model")
            if any(not isinstance(value, str) or not value or len(value) > 1024 for value in model.values()):
                raise ExtensionProtocolError("invalid model")
            frozen_model = MappingProxyType(dict(model))
        return ExtensionSnapshot(
            data["idle"], data["pendingMessages"], frozen_model, _immutable(data["async"]), sequence, "connected", None
        )

    def _apply_event(self, event: Mapping[str, Any]) -> None:
        sequence = event["sequence"]
        if self._sequence and sequence != self._sequence + 1:
            raise ExtensionProtocolError("event sequence gap")
        self._sequence = sequence
        if event["kind"] == "snapshot":
            snapshot = self._snapshot_from_data(dict(event["data"]), sequence)
            with self._state_lock:
                self._snapshot = snapshot
        with self._state_lock:
            self._events.append(event)
        callback = self._on_event
        if callback is not None:
            try:
                callback(event)
            except Exception:
                pass

    def _reader_loop(self) -> None:
        reason = "extension disconnected"
        try:
            while True:
                if self._clock() - self._last_heartbeat > self._heartbeat_timeout:
                    reason = "stale heartbeat"
                    break
                try:
                    frame = self._read_frame()
                except TimeoutError:
                    continue
                if frame.get("type") == "response":
                    self._handle_response(frame)
                else:
                    event = self._parse_event(frame)
                    self._apply_event(event)
                    if event["kind"] == "heartbeat":
                        self._last_heartbeat = self._clock()
                        self._send_pong()
        except ExtensionProtocolError as exc:
            reason = str(exc)
        except (ExtensionDisconnected, OSError):
            pass
        finally:
            self._disconnect(reason)

    def _handle_response(self, frame: dict[str, Any]) -> None:
        _exact_keys(frame, {"protocol", "type", "id", "ok"}, {"result", "error"}, "response")
        request_id = frame["id"]
        if (
            frame["protocol"] != PROTOCOL
            or frame["type"] != "response"
            or not isinstance(request_id, str)
            or not request_id
            or type(frame["ok"]) is not bool
        ):
            raise ExtensionProtocolError("invalid response envelope")
        if (
            frame["ok"]
            and "error" in frame
            or not frame["ok"]
            and ("result" in frame or not isinstance(frame.get("error"), str))
        ):
            raise ExtensionProtocolError("invalid response")
        response = MappingProxyType({key: _immutable(value) for key, value in frame.items()})
        with self._state_lock:
            pending = self._pending.pop(request_id, None)
            if pending is None:  # timeout, duplicate, or unsolicited response
                return
            self._stale.append(request_id)
            pending.response = response
            pending.ready.set()

    def _send_pong(self) -> None:
        # Heartbeat acknowledgement is supervised but must not block the reader.
        with self._state_lock:
            if self._closed or len(self._pending) >= self._response_limit:
                return
            self._next_id += 1
            request_id = str(self._next_id)
        self._send({"protocol": PROTOCOL, "type": "request", "id": request_id, "action": "pong"})

    def _disconnect(self, reason: str) -> None:
        with self._state_lock:
            already_closed = self._closed
            self._closed = True
            self._reason = reason
            sock, self._sock = self._sock, None
            pending = tuple(self._pending.values())
            self._pending.clear()
            if self._snapshot is not None:
                old = self._snapshot
                self._snapshot = ExtensionSnapshot(
                    old.idle, old.pending_messages, old.model, old.async_jobs, old.sequence, "degraded", reason
                )
        for item in pending:
            item.disconnected = True
            item.ready.set()
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        if already_closed:
            return
