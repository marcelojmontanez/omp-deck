from __future__ import annotations

import threading
import unittest
from collections import deque

from omp_deck._collab_transport import _classify_fatal
from omp_deck.collab_adapter import CollabAdapter
from omp_deck.collab_protocol import (
    b64url_encode,
    open_frame,
    pack_envelope,
    seal_frame,
    unpack_envelope,
)
from omp_deck.fleet_model import FleetModel
from omp_deck.rpc_adapter import AdapterDisconnectedError, AdapterError, AdapterIncompatibleError

KEY = bytes(range(32))
TOKEN = bytes(range(16))
ROOM = "abcdefghij"


def link(write=True):
    secret = KEY + (TOKEN if write else b"")
    return f"ws://localhost/r/{ROOM}.{b64url_encode(secret)}"


def host(frame):
    return pack_envelope(7, seal_frame(KEY, frame))


class Socket:
    def __init__(self, incoming):
        self.incoming = deque(incoming)
        self.sent = []
        self.closed = False

    def send(self, value):
        self.sent.append(value)

    def recv(self, timeout=None):
        if self.incoming:
            return self.incoming.popleft()
        threading.Event().wait(timeout or 0.01)
        raise TimeoutError()

    def close(self):
        self.closed = True


class FailingSendSocket(Socket):
    def send(self, value):
        raise OSError("write failed")


WELCOME = {
    "t": "welcome",
    "proto": 3,
    "header": {"type": "session", "id": "s", "timestamp": "now", "cwd": "/x"},
    "state": {"isStreaming": False, "queuedMessageCount": 0, "cwd": "/x", "participants": []},
    "agents": [],
    "entryCount": 1,
}
CHUNK = {
    "t": "snapshot-chunk",
    "entries": [
        {
            "type": "message",
            "id": "e",
            "parentId": None,
            "timestamp": "now",
            "message": {"role": "user", "content": "hi", "timestamp": 0},
        }
    ],
    "final": True,
}


class CollabAdapterTests(unittest.TestCase):
    def make(self, frames, **kw):
        sock = Socket(frames)
        adapter = CollabAdapter(link(), "guest", connector=lambda url, **opts: sock, **kw)
        return adapter, sock

    def test_handshake_snapshot_frames_and_commands(self):
        frames = [
            host(WELCOME),
            host(CHUNK),
            host({"t": "entry", "entry": {"type": "x", "id": "2"}}),
            host({"t": "event", "event": {"type": "turn_start"}}),
            host(
                {
                    "t": "state",
                    "state": {"isStreaming": False, "queuedMessageCount": 0, "cwd": "/y", "participants": []},
                }
            ),
            host(
                {
                    "t": "agents",
                    "agents": [
                        {
                            "id": "a",
                            "displayName": "A",
                            "kind": "sub",
                            "status": "running",
                            "hasSessionFile": False,
                            "createdAt": 0,
                            "lastActivity": 0,
                        }
                    ],
                }
            ),
            host({"t": "bus", "channel": "task:subagent:lifecycle", "data": {"id": "a"}}),
            host({"t": "ui-request", "request": {"kind": "editor", "title": "x", "reqId": 9}}),
            host({"t": "ui-request-end", "reqId": 9}),
        ]
        adapter, sock = self.make(frames)
        adapter.start()
        adapter.send_message("hello", images=[{"type": "image", "data": "AA", "mimeType": "image/png"}])
        adapter.interrupt()
        adapter.agent_chat("a", "yo")
        adapter.agent_kill("a")
        adapter.agent_revive("a")
        peer, sealed = unpack_envelope(sock.sent[0])
        hello = open_frame(KEY, sealed, direction="guest")
        self.assertEqual((peer, hello["proto"], hello["writeToken"]), (0, 3, b64url_encode(TOKEN)))
        snap = adapter.snapshot()
        self.assertEqual(snap.entries[0]["id"], "e")
        with self.assertRaises(TypeError):
            snap.header["id"] = "changed"
        adapter.close()
        self.assertTrue(sock.closed)

    def test_completed_snapshot_emits_connected_transport_event(self):
        events = []
        adapter, _ = self.make([host(WELCOME), host(CHUNK)], on_event=events.append)

        adapter.start()

        self.assertTrue(
            any(event.type == "transport" and event.payload.get("status") == "connected" for event in events)
        )
        adapter.close()

    def test_view_link_denies_all_mutation_before_send(self):
        sock = Socket(
            [
                host({**WELCOME, "entryCount": 0, "readOnly": True}),
                host({"t": "snapshot-chunk", "entries": [], "final": True}),
            ]
        )
        adapter = CollabAdapter(link(False), "viewer", connector=lambda url, **opts: sock)
        adapter.start()
        count = len(sock.sent)
        for operation in (
            lambda: adapter.send_message("x"),
            adapter.interrupt,
            lambda: adapter.agent_kill("a"),
            lambda: adapter.respond_ui(1, "x"),
        ):
            with self.assertRaises(AdapterError):
                operation()
        self.assertEqual(len(sock.sent), count)
        adapter.close()

    def test_duplicate_and_stale_ui_first_response_wins(self):
        adapter, sock = self.make(
            [
                host(WELCOME),
                host(CHUNK),
                host({"t": "ui-request", "request": {"kind": "select", "title": "x", "options": [], "reqId": 3}}),
                host(
                    {"t": "ui-request", "request": {"kind": "select", "title": "duplicate", "options": [], "reqId": 3}}
                ),
            ]
        )
        adapter.start()
        # Reader processing is ordered; accepting removes it, making every later response stale.
        for _ in range(100):
            if adapter.respond_ui(3, "x"):
                break
            threading.Event().wait(0.001)
        self.assertFalse(adapter.respond_ui(3, "y"))
        adapter.close()

    def test_ui_queue_overflow_is_explicit_and_settled_requests_free_capacity(self):
        adapter, _ = self.make([], ui_limit=1)
        adapter._receive(host(WELCOME))
        adapter._receive(host(CHUNK))
        adapter._receive(host({"t": "ui-request", "request": {"kind": "editor", "title": "first", "reqId": 3}}))
        second = host({"t": "ui-request", "request": {"kind": "editor", "title": "second", "reqId": 4}})

        with self.assertRaises(AdapterIncompatibleError) as caught:
            adapter._receive(second)

        self.assertEqual(caught.exception.kind, "bounds")
        self.assertEqual([request["reqId"] for request in adapter.snapshot().pending_ui_requests], [3])
        adapter._receive(host({"t": "ui-request-end", "reqId": 3}))
        adapter._receive(second)
        self.assertEqual([request["reqId"] for request in adapter.snapshot().pending_ui_requests], [4])

    def test_streaming_behavior_is_rejected_before_send(self):
        adapter, sock = self.make([host(WELCOME), host(CHUNK)])
        adapter.start()
        count = len(sock.sent)
        with self.assertRaises(ValueError):
            adapter.send_message("later", streaming_behavior="followUp")
        self.assertEqual(len(sock.sent), count)
        adapter.close()

    def test_public_arguments_are_encoded_before_ui_state_changes(self):
        adapter, _ = self.make([])
        adapter._status = "connected"
        adapter._ws = Socket([])
        adapter._ui[4] = {"reqId": 4, "kind": "confirm", "title": "x"}
        adapter._ui_order.append(4)
        with self.assertRaises(ValueError):
            adapter.respond_ui(True, "yes")
        self.assertIn(4, adapter._ui)

    def test_failed_ui_send_preserves_request_for_retry(self):
        adapter, _ = self.make([])
        adapter._status = "connected"
        adapter._ws = FailingSendSocket([])
        adapter._ui[4] = {"reqId": 4, "kind": "confirm", "title": "x"}
        adapter._ui_order.append(4)
        with self.assertRaises(OSError):
            adapter.respond_ui(4, "yes")
        self.assertIn(4, adapter._ui)
        adapter._ws = Socket([])
        self.assertTrue(adapter.respond_ui(4, "yes"))
        self.assertFalse(adapter.respond_ui(4, "again"))

    def test_live_events_are_normalized_for_fleet_activity(self):
        events = []
        adapter, _ = self.make([], on_event=events.append)
        welcome = {
            **WELCOME,
            "state": {
                **WELCOME["state"],
                "sessionName": "Live terminal",
                "thinkingLevel": "high",
                "model": {
                    "id": "gpt-5",
                    "name": "GPT-5",
                    "provider": "openai",
                    "api": "responses",
                    "contextWindow": 200000,
                },
            },
        }
        adapter._receive(host(welcome))
        adapter._receive(host(CHUNK))

        model = FleetModel()
        model.load_record(
            {
                "id": "deck",
                "title": "Terminal",
                "project": {"name": "Terminal", "path": "/x"},
                "adapterKind": "collab",
                "generation": 1,
                "updatedAt": "2026-01-01T00:00:00+00:00",
            }
        )
        self.assertTrue(
            model.apply_collab_snapshot("deck", instance_id="collab", generation=1, snapshot=adapter.snapshot())
        )
        projected = model.get("deck")
        self.assertEqual(projected["title"], "Live terminal")
        self.assertEqual(projected["thinking"], "high")
        self.assertEqual(
            projected["model"],
            {"provider": "openai", "id": "gpt-5", "name": "GPT-5", "api": "responses"},
        )
        adapter._ingest(
            7,
            {
                "t": "event",
                "event": {
                    "type": "tool_execution_start",
                    "toolCallId": "call-1",
                    "toolName": "read",
                    "args": {"path": "x"},
                    "intent": "inspect",
                },
            },
        )
        event = events[-1]
        self.assertEqual(event.type, "tool_execution_start")
        self.assertEqual(
            event.payload,
            {"tool_call_id": "call-1", "tool_name": "read", "args": {"path": "x"}, "intent": "inspect"},
        )
        self.assertTrue(model.apply_adapter_event("deck", instance_id="collab", generation=1, event=event))
        self.assertEqual(model.get("deck")["workingOn"], "inspect")

    def test_successful_ui_response_emits_authoritative_settlement(self):
        events = []
        adapter, _ = self.make([])
        adapter._status = "connected"
        adapter._ws = Socket([])
        adapter._ui[4] = {"reqId": 4, "kind": "editor", "title": "x"}
        adapter._ui_order.append(4)
        adapter._callback = events.append

        self.assertTrue(adapter.respond_ui(4, "yes"))
        self.assertEqual((events[-1].type, events[-1].payload), ("ui_request_resolved", {"id": "4"}))
        self.assertIsNone(adapter._ingest_ui_request_end({"reqId": 4}))
        self.assertEqual(len(events), 1)

    def test_failed_connection_is_closed_before_reconnect(self):
        class FailingRecvSocket(Socket):
            def __init__(self, peer):
                super().__init__([])
                self.peer = peer

            def recv(self, timeout=None):
                raise OSError("connection lost")

            def close(self):
                self.peer.append("closed")
                super().close()

        observations = []
        sockets = []

        def connector(url, **opts):
            observations.append(tuple(sockets[-1].peer) if sockets else ())
            socket = FailingRecvSocket([])
            sockets.append(socket)
            if len(sockets) == 2:
                adapter._stopping.set()
            return socket

        adapter = CollabAdapter(link(), "guest", connector=connector, random=lambda: 0)
        adapter._stopping.wait = lambda delay: False
        adapter._run()
        self.assertEqual(observations[1], ("closed",))
        self.assertTrue(sockets[0].closed)

    def test_relay_control_text_is_strict_bounded_and_room_close_is_fatal(self):
        adapter, _ = self.make([], max_frame_bytes=128)
        self.assertFalse(adapter._receive('{"type":"peer-joined","peerId":1}'))
        self.assertFalse(adapter._receive('{"type":"peer-left","peerId":1}'))
        for raw in (
            '{"type":"unknown"}',
            '{"type":"peer-left","peerId":true}',
            '{"type":"peer-left","peerId":1,"extra":0}',
            "NaN",
        ):
            with self.assertRaises(AdapterIncompatibleError):
                adapter._receive(raw)
        with self.assertRaises(AdapterDisconnectedError) as caught:
            adapter._receive('{"type":"room-closed","reason":"gone"}')
        self.assertIs(_classify_fatal(caught.exception), caught.exception)
        with self.assertRaises(AdapterIncompatibleError):
            adapter._receive(" " * 129)

    def test_custom_relay_close_codes_are_fatal_and_redacted(self):
        class Closed(Exception):
            def __init__(self, code):
                self.code = code
                super().__init__(f"{code} {b64url_encode(KEY)}")

        adapter, _ = self.make([])
        for code in (4001, 4004, 4009, 4029):
            fatal = _classify_fatal(Closed(code))
            self.assertIsInstance(fatal, AdapterDisconnectedError)
            self.assertNotIn(b64url_encode(KEY), repr(fatal))

    def test_connector_uses_exact_bounded_websocket_options(self):
        seen = {}
        sock = Socket([host(WELCOME), host(CHUNK)])

        def connector(url, **options):
            seen.update(options)
            return sock

        adapter = CollabAdapter(link(), "guest", connector=connector)
        adapter.start()
        self.assertEqual(
            seen,
            {
                "max_size": 1048608,
                "open_timeout": 10.0,
                "close_timeout": 10.0,
                "ping_interval": 20.0,
                "ping_timeout": 20.0,
                "max_queue": 16,
            },
        )
        adapter.close()

    def test_snapshot_bytes_are_compact_utf8_json(self):
        entries = [{"type": "x", "text": "é"}]
        exact = len('[{"type":"x","text":"é"}]'.encode())
        adapter, _ = self.make([])
        adapter._ingest(7, {**WELCOME, "entryCount": 1})
        adapter._max_snapshot_bytes = exact
        adapter._ingest(7, {"t": "snapshot-chunk", "entries": entries, "final": True})
        self.assertEqual(adapter._snapshot_bytes, exact)

    def test_snapshot_chunk_resets_snapshot_timeout(self):
        class TimedSocket(Socket):
            def __init__(self, incoming):
                super().__init__(incoming)
                self.timeouts = []

            def recv(self, timeout=None):
                self.timeouts.append(timeout)
                return super().recv(timeout)

        ticks = iter(float(value) for value in range(0, 105, 5))
        sock = TimedSocket(
            [
                host({**WELCOME, "entryCount": 1}),
                host({"t": "snapshot-chunk", "entries": [], "final": False}),
                host(CHUNK),
            ]
        )
        adapter = CollabAdapter(link(), "guest", connector=lambda url, **opts: sock, clock=lambda: next(ticks))
        adapter.start()
        self.assertEqual(sock.timeouts[:3], [25.0, 20.0, 25.0])
        adapter.close()

    def test_backoff_jitter_range_and_snapshot_resets_delay(self):
        class StopAfterWaits:
            def __init__(self):
                self.waits = []

            def is_set(self):
                return len(self.waits) >= 3

            def wait(self, delay):
                self.waits.append(delay)
                return False

        sockets = iter(
            (
                OSError("first"),
                Socket(
                    [host({**WELCOME, "entryCount": 0}), host({"t": "snapshot-chunk", "entries": [], "final": True})]
                ),
                OSError("third"),
            )
        )

        def connector(url, **opts):
            item = next(sockets)
            if isinstance(item, BaseException):
                raise item
            return item

        adapter = CollabAdapter(link(), "guest", connector=connector, random=lambda: 0.0)
        stop = StopAfterWaits()
        adapter._stopping = stop
        adapter._run()
        self.assertEqual(stop.waits[:2], [0.75, 0.75])

    def test_callback_failure_is_contained(self):
        seen = []

        def bad(event):
            seen.append(event.type)
            raise RuntimeError("boom")

        adapter, _ = self.make(
            [host(WELCOME), host(CHUNK), host({"t": "event", "event": {"type": "agent_start"}})], on_event=bad
        )
        adapter.start()
        self.assertEqual(adapter.transport_status, "connected")
        adapter.close()
        self.assertTrue(seen)

    def test_close_retains_unjoinable_reader_and_reports_failure(self):
        class StuckThread:
            def __init__(self):
                self.alive = True
                self.joined = False

            def join(self, timeout):
                self.joined = True

            def is_alive(self):
                return self.alive

        adapter, sock = self.make([])
        thread = StuckThread()
        adapter._thread = thread
        adapter._ws = sock
        adapter._status = "connected"

        with self.assertRaisesRegex(AdapterError, "reader did not stop"):
            adapter.close()

        self.assertTrue(thread.joined)
        self.assertIs(adapter._thread, thread)
        self.assertEqual(adapter.transport_status, "degraded")

        thread.alive = False
        adapter.close()
        self.assertIsNone(adapter._thread)
        self.assertEqual(adapter.transport_status, "disconnected")

    def test_close_cancels_start_waiting_for_initial_snapshot(self):
        class BlockingSocket(Socket):
            def __init__(self):
                super().__init__([])
                self.receiving = threading.Event()
                self.released = threading.Event()

            def recv(self, timeout=None):
                self.receiving.set()
                self.released.wait(2)
                raise OSError("closed")

            def close(self):
                super().close()
                self.released.set()

        sock = BlockingSocket()
        adapter = CollabAdapter(link(), "guest", connector=lambda url, **opts: sock)
        errors = []

        def start():
            try:
                adapter.start()
            except Exception as error:
                errors.append(error)

        worker = threading.Thread(target=start)
        worker.start()
        self.assertTrue(sock.receiving.wait(1))

        adapter.close()
        worker.join(1)

        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], AdapterDisconnectedError)
        self.assertEqual(adapter.transport_status, "disconnected")

    def test_closed_adapter_refuses_start_without_reopening_transport(self):
        adapter, sock = self.make([host(WELCOME), host(CHUNK)])
        adapter.close()
        with self.assertRaises(AdapterDisconnectedError):
            adapter.start()
        self.assertEqual(sock.sent, [])
        self.assertEqual(adapter.transport_status, "disconnected")

    def test_corrupt_crypto_is_fatal_and_redacted(self):
        corrupt = pack_envelope(1, b"0" * 28)
        adapter, _ = self.make([corrupt])
        with self.assertRaises(AdapterError) as caught:
            adapter.start()
        self.assertNotIn(b64url_encode(KEY), repr(caught.exception))

    def test_bounds_rejected(self):
        adapter, _ = self.make([host({**WELCOME, "entryCount": 2})], entry_limit=1)
        with self.assertRaises(AdapterError):
            adapter.start()

    def test_protocol_order_rejected(self):
        adapter, _ = self.make([host(CHUNK)])
        with self.assertRaises(AdapterError):
            adapter.start()


if __name__ == "__main__":
    unittest.main()
