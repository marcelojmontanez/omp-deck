from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import threading
import time
import unittest
from types import MappingProxyType

from omp_deck._extension_endpoint import unix_endpoint_name
from omp_deck.extension_adapter import (
    PROTOCOL,
    ExtensionAdapter,
    ExtensionAdapterError,
    ExtensionDisconnected,
    ExtensionProtocolError,
    ExtensionRequestTimeout,
)

SESSION = "session-1"
CONNECTION = "connection-1"
PATH = (
    rf"\\.\pipe\{hashlib.sha256(SESSION.encode()).hexdigest()[:24]}.123.{CONNECTION}.pipe"
    if os.name == "nt"
    else f"/tmp/{unix_endpoint_name(SESSION, CONNECTION)}"
)


def registration(**changes):
    value = {
        "protocol": PROTOCOL,
        "version": 1,
        "type": "registration",
        "ompVersion": "9.8.7",
        "compatible": True,
        "connectionId": CONNECTION,
        "session": {"id": SESSION, "file": None, "title": "Title"},
        "sessionId": SESSION,
        "title": "Title",
        "project": "/project",
        "capabilities": {
            "sendMessage": True,
            "followUp": True,
            "interrupt": True,
            "shutdown": True,
            "todos": True,
            "subagents": True,
            "answerUiRequest": False,
            "answer_ui_request": False,
        },
    }
    value.update(changes)
    return value


def thinking_registration(levels=("off", "low", "medium", "high", "xhigh")):
    value = registration()
    value["capabilities"]["setThinking"] = True
    value["thinkingLevels"] = list(levels)
    return value


def image_registration():
    value = registration()
    value["capabilities"]["attachments"] = True
    return value


def thinking_snapshot(thinking="high", levels=("off", "low", "medium", "high", "xhigh")):
    return event(
        data={
            "idle": True,
            "pendingMessages": False,
            "model": {"provider": "openai", "id": "gpt"},
            "async": [],
            "thinking": thinking,
            "thinkingLevels": list(levels),
        }
    )


def event(sequence=1, kind="snapshot", data=None):
    if data is None:
        data = {"idle": True, "pendingMessages": False, "model": {"provider": "p", "id": "m"}, "async": [{"id": "job"}]}
    return {"protocol": PROTOCOL, "type": "event", "sequence": sequence, "kind": kind, "data": data}


def line(value):
    return json.dumps(value, separators=(",", ":")).encode() + b"\n"


class Peer:
    def __init__(self, initial=(registration(), event())):
        self.client, self.server = socket.socketpair()
        self.server.sendall(b"".join(line(item) for item in initial))
        self.file = self.server.makefile("rb")

    def connector(self, _path):
        return self.client

    def read(self):
        return json.loads(self.file.readline())

    def send(self, value):
        self.server.sendall(line(value))

    def close(self):
        self.file.close()
        self.server.close()


def response(request, *, ok=True, result=None):
    value = {"protocol": PROTOCOL, "type": "response", "id": request["id"], "ok": ok}
    if ok and result is not None:
        value["result"] = result
    if not ok:
        value["error"] = "private plugin details"
    return value


def _capture(errors, function):
    try:
        function()
    except Exception as exc:
        errors.append(exc)


class ExtensionAdapterTests(unittest.TestCase):
    def make_peer(self, initial=(registration(), event())):
        peer = Peer(initial)
        self.addCleanup(peer.close)
        return peer

    def make_adapter(self, peer=None, **options):
        if peer is not None:
            options["connector"] = peer.connector
        adapter = ExtensionAdapter(PATH, **options)
        self.addCleanup(adapter.close)
        return adapter

    def stop_and_join(self, adapter, thread):
        adapter.close()
        thread.join(1)

    def test_registration_version_and_authoritative_snapshot(self):
        peer = self.make_peer()
        adapter = self.make_adapter(peer)
        got = adapter.start()
        try:
            self.assertEqual(got.protocol_version, 1)
            self.assertEqual(got.omp_version, "9.8.7")
            self.assertEqual(got.session_id, SESSION)
            self.assertEqual(got.connection_id, CONNECTION)
            self.assertIsInstance(got.capabilities, MappingProxyType)
            snapshot = adapter.snapshot()
            self.assertIsNotNone(snapshot)
            self.assertEqual((snapshot.idle, snapshot.pending_messages, snapshot.sequence), (True, False, 1))
            self.assertEqual(snapshot.transport_status, "connected")
            self.assertIsNone(snapshot.thinking)
            self.assertEqual(snapshot.thinking_levels, ())
            self.assertNotIn("setThinking", got.capabilities)
            self.assertNotIn("attachments", got.capabilities)
        finally:
            adapter.close()
            peer.close()

    def test_connection_token_is_validated_and_sent_before_registration(self):
        for token in ("short", "A" * 64, 1):
            with self.subTest(token=token):
                with self.assertRaises(ValueError):
                    ExtensionAdapter(PATH, connection_token=token)

        peer = self.make_peer()
        adapter = self.make_adapter(peer, connection_token="a" * 64)
        adapter.start()
        self.assertEqual(
            peer.read(),
            {
                "protocol": PROTOCOL,
                "version": 1,
                "type": "connect",
                "token": "a" * 64,
            },
        )

    def test_start_rejects_wrong_registration(self):
        cases = [
            (registration(version=2), "incompatible"),
            (registration(connectionId="other"), "socket identity"),
            (event(), "registration"),
        ]
        for first, match in cases:
            with self.subTest(first=first, match=match):
                peer = self.make_peer((first, event()))
                with self.assertRaisesRegex(ExtensionProtocolError, match):
                    self.make_adapter(peer).start()

    def test_registration_rejects_unadvertised_or_noncanonical_thinking_choices(self):
        unadvertised = registration(thinkingLevels=["low"])
        reversed_levels = thinking_registration(("high", "low"))
        for value in (unadvertised, reversed_levels):
            with self.subTest(registration=value):
                peer = self.make_peer((value, event()))
                with self.assertRaises(ExtensionProtocolError):
                    self.make_adapter(peer).start()

    def test_start_requires_snapshot_as_second_frame(self):
        peer = self.make_peer((registration(), event(kind="heartbeat", data={"time": 1})))
        with self.assertRaisesRegex(ExtensionProtocolError, "snapshot"):
            self.make_adapter(peer).start()
        peer.close()

    def test_new_registration_exposes_effective_thinking_and_model_choices(self):
        peer = self.make_peer((thinking_registration(), thinking_snapshot()))
        adapter = self.make_adapter(peer)

        registration_value = adapter.start()
        snapshot = adapter.snapshot()

        self.assertTrue(registration_value.capabilities["setThinking"])
        self.assertEqual(registration_value.thinking_levels, ("off", "low", "medium", "high", "xhigh"))
        self.assertEqual(snapshot.thinking, "high")
        self.assertEqual(snapshot.thinking_levels, registration_value.thinking_levels)

    def test_set_thinking_is_correlated_and_uses_authoritative_clamped_result(self):
        peer = self.make_peer((thinking_registration(), thinking_snapshot()))
        adapter = self.make_adapter(peer)
        adapter.start()
        result = []
        thread = threading.Thread(target=lambda: result.append(adapter.set_thinking("xhigh")))
        thread.start()
        self.addCleanup(self.stop_and_join, adapter, thread)

        request = peer.read()
        self.assertEqual(
            {key: request[key] for key in ("protocol", "type", "action", "level")},
            {"protocol": PROTOCOL, "type": "request", "action": "setThinking", "level": "xhigh"},
        )
        peer.send(
            response(
                request,
                result={
                    "thinking": "high",
                    "thinkingLevels": ["off", "low", "medium", "high", "xhigh"],
                    "eventSequence": 1,
                },
            )
        )
        thread.join(1)

        self.assertEqual(result, ["high"])
        self.assertEqual(adapter.snapshot().thinking, "high")
        with self.assertRaisesRegex(ValueError, "unsupported"):
            adapter.set_thinking("max")

    def test_image_messages_preserve_content_order_and_delivery_mode(self):
        peer = self.make_peer((image_registration(), event()))
        adapter = self.make_adapter(peer)
        adapter.start()
        png = {"type": "image", "data": "iVBORw0KGgo=", "mimeType": "image/png"}
        results = []
        threads = [
            threading.Thread(target=lambda: results.append(adapter.send_message("", images=[png]))),
            threading.Thread(target=lambda: results.append(adapter.send_message("inspect", "followUp", images=[png]))),
        ]
        for thread in threads:
            thread.start()
            self.addCleanup(self.stop_and_join, adapter, thread)
        requests = [peer.read() for _ in threads]
        by_action = {request["action"]: request for request in requests}
        self.assertEqual(by_action["sendMessage"]["message"], "")
        self.assertEqual(by_action["sendMessage"]["images"], [png])
        self.assertEqual(by_action["followUp"]["message"], "inspect")
        self.assertEqual(by_action["followUp"]["images"], [png])
        for request in requests:
            peer.send(response(request, result={"acceptance": "accepted"}))
        for thread in threads:
            thread.join(1)
        self.assertEqual(results, [{"acceptance": "accepted"}, {"acceptance": "accepted"}])

    def test_image_boundary_rejects_legacy_malformed_oversized_and_over_frame_content(self):
        peer = self.make_peer((image_registration(), event()))
        adapter = self.make_adapter(peer)
        adapter.start()
        png = {"type": "image", "data": "iVBORw0KGgo=", "mimeType": "image/png"}
        legacy_peer = self.make_peer()
        legacy = self.make_adapter(legacy_peer)
        legacy.start()
        with self.assertRaisesRegex(ExtensionAdapterError, "unavailable"):
            legacy.send_message("", images=[png])
        with self.assertRaisesRegex(ValueError, "invalid image"):
            adapter.send_message(
                "",
                images=[{"type": "image", "data": "%%%", "mimeType": "image/png"}],
            )
        oversized = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * (700 * 1024)).decode()
        with self.assertRaisesRegex(ValueError, "image"):
            adapter.send_message(
                "",
                images=[{"type": "image", "data": oversized, "mimeType": "image/png"}],
            )

        constrained_peer = self.make_peer((image_registration(), event()))
        constrained = self.make_adapter(constrained_peer, frame_limit=2048)
        constrained.start()
        near_frame = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * 800).decode()
        with self.assertRaisesRegex(ValueError, "frame exceeds"):
            constrained.send_message(
                "x" * 900,
                images=[{"type": "image", "data": near_frame, "mimeType": "image/png"}],
            )
        result = []
        thread = threading.Thread(target=lambda: result.append(constrained.interrupt()))
        thread.start()
        self.addCleanup(self.stop_and_join, constrained, thread)
        request = constrained_peer.read()
        self.assertEqual(request["action"], "interrupt")
        constrained_peer.send(response(request, result="still connected"))
        thread.join(1)
        self.assertEqual(result, ["still connected"])

    def test_set_thinking_does_not_overwrite_a_newer_event_with_a_stale_response(self):
        peer = self.make_peer((thinking_registration(), thinking_snapshot()))
        adapter = self.make_adapter(peer)
        adapter.start()
        result = []
        thread = threading.Thread(target=lambda: result.append(adapter.set_thinking("xhigh")))
        thread.start()
        self.addCleanup(self.stop_and_join, adapter, thread)

        request = peer.read()
        peer.send(
            event(
                sequence=2,
                kind="thinking",
                data={"thinking": "low", "thinkingLevels": ["off", "low"]},
            )
        )
        peer.send(
            response(
                request,
                result={
                    "thinking": "xhigh",
                    "thinkingLevels": ["off", "low", "medium", "high", "xhigh"],
                    "eventSequence": 1,
                },
            )
        )
        thread.join(1)

        snapshot = adapter.snapshot()
        self.assertEqual(result, ["low"])
        self.assertEqual(snapshot.thinking, "low")
        self.assertEqual(snapshot.thinking_levels, ("off", "low"))
        self.assertEqual(snapshot.sequence, 2)

    def test_command_bytes_and_response_correlation(self):
        peer = self.make_peer()
        adapter = self.make_adapter(peer)
        adapter.start()
        results = {}

        def invoke(name, function):
            results[name] = function()

        threads = [
            threading.Thread(target=invoke, args=("message", lambda: adapter.send_message("hello"))),
            threading.Thread(target=invoke, args=("follow", lambda: adapter.send_message("later", "followUp"))),
            threading.Thread(target=invoke, args=("interrupt", adapter.interrupt)),
            threading.Thread(target=invoke, args=("shutdown", adapter.shutdown_session)),
        ]
        for thread in threads:
            thread.start()
            self.addCleanup(self.stop_and_join, adapter, thread)
        requests = [peer.read() for _ in threads]
        self.assertEqual({item["action"] for item in requests}, {"sendMessage", "followUp", "interrupt", "shutdown"})
        self.assertTrue(
            all(
                item["protocol"] == PROTOCOL and item["type"] == "request" and isinstance(item["id"], str)
                for item in requests
            )
        )
        by_action = {item["action"]: item for item in requests}
        self.assertEqual(by_action["sendMessage"]["message"], "hello")
        self.assertEqual(by_action["followUp"]["message"], "later")
        for request in reversed(requests):
            peer.send(response(request, result=request["action"]))
        for thread in threads:
            thread.join(1)
        self.assertEqual(
            results, {"message": "sendMessage", "follow": "followUp", "interrupt": "interrupt", "shutdown": "shutdown"}
        )
        adapter.close()
        peer.close()

    def test_refresh_uses_snapshot_response(self):
        peer = self.make_peer()
        adapter = self.make_adapter(peer)
        adapter.start()
        holder = []
        thread = threading.Thread(target=lambda: holder.append(adapter.refresh()))
        thread.start()
        self.addCleanup(self.stop_and_join, adapter, thread)
        request = peer.read()
        self.assertEqual(request["action"], "snapshot")
        peer.send(
            response(
                request, result={"idle": False, "pendingMessages": True, "model": None, "async": [], "eventSequence": 1}
            )
        )
        thread.join(1)
        self.assertTrue(holder[0].pending_messages)
        adapter.close()
        peer.close()

    def test_malformed_event_degrades_connection(self):
        cases = [
            {"protocol": PROTOCOL, "type": "event", "sequence": "2", "kind": "x", "data": {}},
            {"protocol": PROTOCOL, "type": "event", "sequence": 2, "kind": "x", "data": {}, "extra": 1},
            {"protocol": PROTOCOL, "type": "event", "sequence": 2, "kind": "", "data": {}},
        ]
        for bad in cases:
            with self.subTest(bad=bad):
                peer = self.make_peer()
                adapter = self.make_adapter(peer)
                adapter.start()
                peer.send(bad)
                deadline = time.monotonic() + 1
                while adapter.snapshot().transport_status == "connected" and time.monotonic() < deadline:
                    time.sleep(0.005)
                self.assertEqual(adapter.snapshot().transport_status, "degraded")
                adapter.close()
                peer.close()

    def test_callback_projection_failure_degrades_and_disconnects(self):
        peer = self.make_peer()

        def fail_projection(_event):
            raise TypeError("not JSON serializable")

        adapter = self.make_adapter(peer, on_event=fail_projection)
        with self.assertRaisesRegex(ExtensionProtocolError, "event callback failed"):
            adapter.start()
        snapshot = adapter.snapshot()
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.transport_status, "degraded")
        self.assertEqual(snapshot.reason, "startup failed")

    def test_frame_bound_checked_and_sequence_gap_is_explicit(self):
        peer = self.make_peer()
        adapter = self.make_adapter(peer, frame_limit=1024)
        adapter.start()
        peer.send(event(3, "message", {}))
        deadline = time.monotonic() + 1
        while adapter.snapshot().transport_status == "connected" and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertEqual(adapter.snapshot().reason, "event sequence gap")
        adapter.close()
        peer.close()

        oversized = self.make_peer(())
        oversized.server.sendall(b"x" * 1024)
        with self.assertRaisesRegex(ExtensionProtocolError, "exceeds"):
            self.make_adapter(oversized, frame_limit=1024).start()
        oversized.close()

    def test_timeout_disconnect_and_stale_duplicate_response(self):
        peer = self.make_peer()
        disconnected = []
        disconnect_ready = threading.Event()

        def on_disconnect(reason):
            disconnected.append(reason)
            disconnect_ready.set()

        adapter = self.make_adapter(peer, registration_timeout=0.03, on_disconnect=on_disconnect)
        adapter.start()
        with self.assertRaises(ExtensionRequestTimeout):
            adapter.interrupt()
        stale = peer.read()
        peer.send(response(stale))

        result = []
        thread = threading.Thread(target=lambda: result.append(adapter.send_message("ok")))
        thread.start()
        self.addCleanup(self.stop_and_join, adapter, thread)
        current = peer.read()
        peer.send(response(current, result="accepted"))
        peer.send(response(current, result="duplicate"))
        thread.join(1)
        self.assertEqual(result, ["accepted"])

        adapter._registration_timeout = 0.2
        errors = []
        thread = threading.Thread(target=lambda: _capture(errors, adapter.shutdown_session))
        thread.start()
        self.addCleanup(self.stop_and_join, adapter, thread)
        peer.read()
        peer.close()
        thread.join(1)
        self.assertIsInstance(errors[0], ExtensionDisconnected)
        self.assertTrue(disconnect_ready.wait(1))
        self.assertEqual(disconnected, ["extension disconnected"])
        adapter.close()

    def test_failed_response_does_not_expose_remote_error(self):
        peer = self.make_peer()
        adapter = self.make_adapter(peer)
        adapter.start()
        errors = []
        thread = threading.Thread(target=lambda: _capture(errors, adapter.interrupt))
        thread.start()
        self.addCleanup(self.stop_and_join, adapter, thread)
        request = peer.read()
        peer.send(response(request, ok=False))
        thread.join(1)
        self.assertIsInstance(errors[0], ExtensionAdapterError)
        self.assertNotIn("private plugin details", str(errors[0]))
        adapter.close()
        peer.close()

    def test_heartbeat_sends_pong_and_stale_heartbeat_disconnects(self):
        peer = self.make_peer()
        now = [0.0]
        adapter = self.make_adapter(peer, clock=lambda: now[0], heartbeat_timeout=2)
        adapter.start()
        peer.send(event(2, "heartbeat", {"time": 10}))
        pong = peer.read()
        self.assertEqual(pong["action"], "pong")
        peer.send(response(pong))
        now[0] = 3.0
        deadline = time.monotonic() + 2
        while adapter.snapshot().transport_status == "connected" and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(adapter.snapshot().reason, "stale heartbeat")
        adapter.close()
        peer.close()

    def test_heartbeat_reconciles_external_thinking_and_model_choices(self):
        peer = self.make_peer((thinking_registration(), thinking_snapshot()))
        adapter = self.make_adapter(peer)
        adapter.start()

        peer.send(
            event(
                2,
                "heartbeat",
                {
                    "time": 10,
                    "thinking": "low",
                    "thinkingLevels": ["off", "minimal", "low"],
                },
            )
        )
        pong = peer.read()
        deadline = time.monotonic() + 1
        while adapter.snapshot().sequence < 2 and time.monotonic() < deadline:
            time.sleep(0.005)

        self.assertEqual(pong["action"], "pong")
        self.assertEqual(adapter.snapshot().thinking, "low")
        self.assertEqual(adapter.snapshot().thinking_levels, ("off", "minimal", "low"))

    def test_callback_values_are_deeply_immutable(self):
        peer = self.make_peer()
        seen = []
        adapter = self.make_adapter(peer, on_event=seen.append)
        adapter.start()
        peer.send(event(2, "message", {"nested": [{"secret": "value"}]}))
        deadline = time.monotonic() + 1
        while len(seen) < 2 and time.monotonic() < deadline:
            time.sleep(0.005)
        drained = adapter.drain_events()
        self.assertEqual(len(drained), 2)
        with self.assertRaises(TypeError):
            drained[-1]["data"]["nested"][0]["secret"] = "changed"
        snapshot = adapter.snapshot()
        with self.assertRaises(TypeError):
            snapshot.model["id"] = "changed"
        with self.assertRaises(TypeError):
            snapshot.async_jobs[0]["id"] = "changed"
        adapter.close()
        peer.close()

    def test_event_queue_is_bounded_and_close_joins_supervised_reader(self):
        peer = self.make_peer()
        adapter = self.make_adapter(peer, event_limit=2)
        adapter.start()
        peer.send(event(2, "one", {}))
        peer.send(event(3, "two", {}))
        deadline = time.monotonic() + 1
        while adapter.snapshot().sequence == 1 and time.monotonic() < deadline:
            time.sleep(0.005)
        adapter.close()
        self.assertIsNotNone(adapter._reader)
        self.assertTrue(adapter._reader.daemon)
        self.assertFalse(adapter._reader.is_alive())
        self.assertEqual([item["sequence"] for item in adapter.drain_events()], [2, 3])
        peer.close()

    def test_strict_public_inputs(self):
        with self.assertRaises(ValueError):
            ExtensionAdapter("")
        with self.assertRaises(TypeError):
            ExtensionAdapter(PATH, on_event=object())
        adapter = self.make_adapter()
        with self.assertRaises(ValueError):
            adapter.send_message("")
        with self.assertRaises(ValueError):
            adapter.send_message("x", "queue")


if __name__ == "__main__":
    unittest.main()
