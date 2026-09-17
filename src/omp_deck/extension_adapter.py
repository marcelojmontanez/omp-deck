"""Synchronous client for the OMP Deck fallback extension protocol."""

from __future__ import annotations

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
    parse_registration,
    parse_response,
    parse_thinking_state,
    snapshot_from_data,
)
from .attachments import validated_image_content

PROTOCOL = "omp-deck-fallback-v1"
_MISSING = object()
_CONNECTION_TOKEN = re.compile(r"[0-9a-f]{64}")


def _default_connector(path: str):
    if os.name == "nt":
        from ._windows_pipe import connect_named_pipe

        return connect_named_pipe(path)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(path)
    except Exception:
        sock.close()
        raise
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
        self._thinking_levels: tuple[str, ...] = ()
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
            registration = parse_registration(self._read_frame(), self.socket_path, PROTOCOL)
            self._registration = registration
            self._thinking_levels = registration.thinking_levels
            snapshot_frame = self._parse_event(self._read_frame(), require_snapshot=True)
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

    def send_message(
        self,
        message: str,
        streaming_behavior: str | None = None,
        *,
        images: Any = None,
    ) -> Any:
        normalized_images = validated_image_content(images)
        if (
            not isinstance(message, str)
            or (not message and not normalized_images)
            or len(message.encode("utf-8")) > self._frame_limit // 2
        ):
            raise ValueError("message content must be non-empty and bounded")
        if streaming_behavior not in (None, "steer", "followUp"):
            raise ValueError("streaming_behavior must be None, 'steer', or 'followUp'")
        if normalized_images:
            registration = self.registration
            if registration is None or registration.capabilities.get("attachments") is not True:
                raise ExtensionAdapterError("image attachments are unavailable")
        fields: dict[str, Any] = {"message": message}
        if normalized_images:
            fields["images"] = normalized_images
        return self._request("followUp" if streaming_behavior == "followUp" else "sendMessage", **fields)

    def interrupt(self) -> Any:
        return self._request("interrupt")

    def shutdown_session(self) -> Any:
        return self._request("shutdown")

    def set_thinking(self, level: str) -> str | None:
        with self._state_lock:
            registration = self._registration
            levels = self._thinking_levels
        if registration is None or registration.capabilities.get("setThinking") is not True:
            raise ExtensionAdapterError("thinking control is unavailable")
        if level not in levels:
            raise ValueError("unsupported thinking level")
        result = _object(self._request("setThinking", level=level), "thinking response")
        _exact_keys(
            result,
            {"thinking", "thinkingLevels", "eventSequence"},
            set(),
            "thinking response",
        )
        event_sequence = result["eventSequence"]
        if type(event_sequence) is not int or event_sequence < 1:
            raise ExtensionProtocolError("invalid thinking response sequence")
        thinking, updated_levels = parse_thinking_state(result)
        with self._state_lock:
            if event_sequence < self._sequence:
                if self._snapshot is None:
                    raise ExtensionProtocolError("stale thinking response")
                return self._snapshot.thinking
            if event_sequence > self._sequence:
                raise ExtensionProtocolError("future thinking response")
            self._thinking_levels = updated_levels
            if self._snapshot is not None:
                old = self._snapshot
                self._snapshot = ExtensionSnapshot(
                    old.idle,
                    old.pending_messages,
                    old.model,
                    old.async_jobs,
                    old.sequence,
                    old.transport_status,
                    old.reason,
                    thinking,
                    updated_levels,
                )
        return thinking

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
            snapshot = snapshot_from_data(result, event_sequence, self._registration, self._thinking_levels)
            self._thinking_levels = snapshot.thinking_levels
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
            encoded = self._encoded_frame(frame)
        except Exception:
            with self._state_lock:
                self._pending.pop(request_id, None)
            raise
        try:
            self._send_encoded(encoded)
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

    def _encoded_frame(self, frame: Mapping[str, Any]) -> bytes:
        encoded = json.dumps(frame, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"
        if len(encoded) > self._frame_limit:
            raise ValueError("request frame exceeds limit")
        return encoded

    def _send_encoded(self, encoded: bytes) -> None:
        with self._send_lock:
            sock = self._sock
            if sock is None:
                raise OSError("closed")
            sock.sendall(encoded)

    def _send(self, frame: Mapping[str, Any]) -> None:
        self._send_encoded(self._encoded_frame(frame))

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

    def _apply_event(self, event: Mapping[str, Any]) -> None:
        sequence = event["sequence"]
        if self._sequence and sequence != self._sequence + 1:
            raise ExtensionProtocolError("event sequence gap")
        self._sequence = sequence
        if event["kind"] == "snapshot":
            snapshot = snapshot_from_data(dict(event["data"]), sequence, self._registration, self._thinking_levels)
            with self._state_lock:
                self._snapshot = snapshot
                self._thinking_levels = snapshot.thinking_levels
        elif event["kind"] in {"heartbeat", "thinking"}:
            data = _object(event["data"], f"{event['kind']} data")
            if "thinking" in data or "thinkingLevels" in data:
                if "thinking" not in data or "thinkingLevels" not in data:
                    raise ExtensionProtocolError("incomplete thinking state")
                if self._registration is None or self._registration.capabilities.get("setThinking") is not True:
                    raise ExtensionProtocolError("unadvertised thinking state")
                thinking, levels = parse_thinking_state(data)
                with self._state_lock:
                    self._thinking_levels = levels
                    if self._snapshot is not None:
                        old = self._snapshot
                        self._snapshot = ExtensionSnapshot(
                            old.idle,
                            old.pending_messages,
                            old.model,
                            old.async_jobs,
                            sequence,
                            old.transport_status,
                            old.reason,
                            thinking,
                            levels,
                        )
        with self._state_lock:
            self._events.append(event)
        callback = self._on_event
        if callback is not None:
            try:
                callback(event)
            except Exception as error:
                raise ExtensionProtocolError("event callback failed") from error

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
        response = parse_response(frame, PROTOCOL)
        request_id = response["id"]
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
                    old.idle,
                    old.pending_messages,
                    old.model,
                    old.async_jobs,
                    old.sequence,
                    "degraded",
                    reason,
                    old.thinking,
                    old.thinking_levels,
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
