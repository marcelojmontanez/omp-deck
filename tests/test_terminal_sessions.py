from __future__ import annotations

import hashlib
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from collections import deque
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from unittest.mock import patch

from omp_deck._collab_imports import CollabImportError
from omp_deck._extension_endpoint import unix_endpoint_name
from omp_deck._extension_protocol import ExtensionDisconnected
from omp_deck._terminal_commands import TerminalCommandConflict
from omp_deck._terminal_discovery import discover_extension_candidates
from omp_deck.collab_adapter import CollabAdapter, CollabSnapshot
from omp_deck.collab_protocol import b64url_encode, open_frame, pack_envelope, seal_frame, unpack_envelope
from omp_deck.collab_store import CollabStore
from omp_deck.extension_adapter import ExtensionAdapter as RealExtensionAdapter
from omp_deck.extension_adapter import ExtensionRegistration, ExtensionSnapshot
from omp_deck.fleet_model import FleetModel
from omp_deck.registry_store import RegistryStore
from omp_deck.rpc_adapter import AdapterError, AdapterEvent
from omp_deck.settings import Settings
from omp_deck.terminal_sessions import TerminalSessionManager, _terminal_id

KEY = bytes(range(32))
TOKEN = bytes(range(16))
ROOM = "abcdefghij"


def collab_link(write=True):
    secret = KEY + (TOKEN if write else b"")
    return f"ws://localhost/r/{ROOM}.{b64url_encode(secret)}"


def host(frame):
    return pack_envelope(7, seal_frame(KEY, frame))


def extension_event(sequence, kind="snapshot", data=None):
    if data is None:
        data = {"idle": True, "pendingMessages": False, "model": None, "async": []}
    return {"protocol": "omp-deck-fallback-v1", "type": "event", "sequence": sequence, "kind": kind, "data": data}


def extension_response(request):
    return {"protocol": "omp-deck-fallback-v1", "type": "response", "id": request["id"], "ok": True}


def extension_line(value):
    return json.dumps(value, separators=(",", ":")).encode() + b"\n"


WELCOME = {
    "t": "welcome",
    "proto": 3,
    "header": {"type": "session", "id": "omp-collab", "timestamp": "now", "cwd": "/work/demo", "title": "Demo"},
    "state": {"isStreaming": False, "queuedMessageCount": 0, "cwd": "/work/demo", "participants": []},
    "agents": [],
    "entryCount": 0,
}
EMPTY_CHUNK = {"t": "snapshot-chunk", "entries": [], "final": True}


class CollabSocket:
    def __init__(self, frames):
        self.incoming = deque(frames)
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


class FakeCollabAdapter:
    instances = []
    fail_start = False
    fail_close = False

    def __init__(self, link, name, on_event=None):
        self._link_for_assertion = link
        self._callback = on_event
        self.calls = []
        self.closed = False
        self.__class__.instances.append(self)
        self._snapshot = CollabSnapshot(
            header=MappingProxyType({"id": "omp-collab", "title": "Collab title"}),
            state=MappingProxyType(
                {"cwd": "/work/collab", "isStreaming": False, "model": {"provider": "p", "id": "m"}}
            ),
            entries=(),
            agents=(),
            pending_ui_requests=(),
            sequence=1,
            transport_status="connected",
            reason=None,
            read_only=False,
        )

    def start(self):
        self.calls.append(("start",))
        if type(self).fail_start:
            raise RuntimeError("connect failed")

    def snapshot(self):
        return self._snapshot

    def send_message(self, message, images=None):
        self.calls.append(("send_message", message, images))

    def interrupt(self):
        self.calls.append(("interrupt",))

    def respond_ui(self, request_id, value):
        self.calls.append(("respond_ui", request_id, value))
        return True

    def close(self):
        self.calls.append(("close",))
        if type(self).fail_close:
            raise RuntimeError("close failed")
        self.closed = True


class FakeExtensionAdapter:
    instances = []
    fail_start = False
    fail_close = False
    fail_sync = False
    sync_errors = deque()
    capabilities = {
        "sendMessage": True,
        "followUp": True,
        "interrupt": True,
        "shutdown": True,
        "todos": True,
        "subagents": True,
        "answerUiRequest": False,
        "setThinking": True,
    }

    def __init__(self, socket_path, on_event=None):
        self.socket_path = socket_path
        self.callback = on_event
        self.calls = []
        self.closed = False
        self.registration = ExtensionRegistration(
            protocol_version=1,
            omp_version="9.8.7",
            session_id="omp-extension",
            project="/work/project",
            title="Extension title",
            connection_id="connection-1",
            capabilities=MappingProxyType(dict(self.capabilities)),
            thinking_levels=("off", "low", "medium", "high", "xhigh"),
        )
        self._snapshot = ExtensionSnapshot(
            idle=True,
            pending_messages=False,
            model=MappingProxyType({"provider": "openai", "id": "gpt"}),
            async_jobs=({"id": "agent-1", "status": "running"},),
            sequence=1,
            transport_status="connected",
            reason=None,
            thinking="high",
            thinking_levels=("off", "low", "medium", "high", "xhigh"),
        )
        self._refresh_snapshot = None
        self.__class__.instances.append(self)

    def start(self):
        self.calls.append(("start",))
        if type(self).fail_start:
            raise RuntimeError("connect failed")
        return self.registration

    def snapshot(self):
        return self._snapshot

    def refresh(self):
        self.calls.append(("refresh",))
        if self._refresh_snapshot is not None:
            self._snapshot = self._refresh_snapshot
        return self._snapshot

    def sync_history(self):
        self.calls.append(("sync_history",))
        if type(self).sync_errors:
            raise type(self).sync_errors.popleft()
        if type(self).fail_sync:
            raise RuntimeError("sync failed")

    def send_message(self, message, behavior=None):
        self.calls.append(("send_message", message, behavior))

    def interrupt(self):
        self.calls.append(("interrupt",))

    def shutdown_session(self):
        self.calls.append(("shutdown_session",))

    def set_thinking(self, level):
        self.calls.append(("set_thinking", level))
        self._snapshot = replace(self._snapshot, thinking=level)
        if self._refresh_snapshot is not None:
            self._refresh_snapshot = replace(self._refresh_snapshot, thinking=level)
        return level

    def close(self):
        self.calls.append(("close",))
        if type(self).fail_close:
            raise RuntimeError("close failed")
        self.closed = True


class TerminalSessionManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/tmp" if sys.platform == "darwin" else None)
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        socket_root = root / "sockets"
        state_root = root / "state"
        socket_root.mkdir(mode=0o700)
        state_root.mkdir(mode=0o700)
        self.settings = Settings(
            host="127.0.0.1",
            port=8787,
            session_root=root / "sessions",
            socket_root=socket_root,
            state_root=state_root,
            project_root=root / "project",
            project_discovery_depth=2,
            omp_bin="omp",
            allowed_hosts=("127.0.0.1",),
        )
        self.model = FleetModel()
        self.managers = []
        FakeCollabAdapter.instances.clear()
        FakeCollabAdapter.fail_start = False
        FakeCollabAdapter.fail_close = False
        FakeExtensionAdapter.instances.clear()
        FakeExtensionAdapter.fail_start = False
        FakeExtensionAdapter.fail_close = False
        FakeExtensionAdapter.fail_sync = False
        FakeExtensionAdapter.sync_errors.clear()

    def tearDown(self):
        for manager in reversed(self.managers):
            manager.close()

    def manager(self, **kwargs):
        manager = TerminalSessionManager(self.settings, self.model, **kwargs)
        self.managers.append(manager)
        return manager

    def extension_candidates(self, *, open_file=os.open):
        return discover_extension_candidates(
            self.settings.socket_root,
            open_file=open_file,
            effective_uid=os.geteuid(),
        )

    def unix_registration(self, session="omp-extension", connection="connection-1", **changes):
        path = self.settings.socket_root / unix_endpoint_name(session, connection)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(path))
        self.addCleanup(listener.close)
        metadata = {
            "protocol": "omp-deck-fallback-v1",
            "version": 1,
            "type": "registration",
            "sessionId": session,
            "connectionId": connection,
            "project": "/work/project",
            "title": "Extension title",
        }
        metadata.update(changes)
        metadata_path = Path(str(path) + ".json")
        metadata_path.write_text(json.dumps(metadata, separators=(",", ":")), encoding="utf-8")
        metadata_path.chmod(0o600)
        return path, metadata_path, metadata

    def connect_extension(self, manager, session="omp-extension", connection="connection-1"):
        path, _, metadata = self.unix_registration(session, connection)
        with patch("omp_deck.terminal_sessions.ExtensionAdapter", FakeExtensionAdapter):
            candidates = self.extension_candidates()
            self.assertEqual(candidates, {session: (path, metadata)})
            manager._reconcile_extensions(candidates)
        return _terminal_id(session), path, FakeExtensionAdapter.instances[-1]

    def test_metadata_discovery_uses_socket_identity_without_proc_and_rejects_unsafe_files(self):
        good_path, _, metadata = self.unix_registration()

        target = self.settings.socket_root / "target.sock.json"
        target.write_text(json.dumps(metadata), encoding="utf-8")
        (self.settings.socket_root / "symlink.sock.json").symlink_to(target)
        (self.settings.socket_root / "oversize.sock.json").write_bytes(b" " * (64 * 1024 + 1))

        legacy_session = "different"
        legacy_connection = "connection-x"
        legacy_socket_id = hashlib.sha256(legacy_session.encode()).hexdigest()[:24]
        wrong_path = self.settings.socket_root / f"{legacy_socket_id}.123.{legacy_connection}.sock"
        wrong = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        wrong.bind(str(wrong_path))
        self.addCleanup(wrong.close)
        legacy_metadata_path = Path(str(wrong_path) + ".json")
        legacy_metadata_path.write_text(
            json.dumps(
                {
                    "protocol": "omp-deck-fallback-v1",
                    "version": 1,
                    "type": "registration",
                    "sessionId": legacy_session,
                    "connectionId": legacy_connection,
                }
            ),
            encoding="utf-8",
        )
        legacy_metadata_path.chmod(0o600)
        regular = self.settings.socket_root / "regular.123.connection-y.sock"
        regular.write_text("not a socket", encoding="utf-8")
        Path(str(regular) + ".json").write_text(
            json.dumps({"sessionId": "regular", "connectionId": "connection-y"}), encoding="utf-8"
        )

        real_open = os.open

        def guarded_open(path, flags, *args, **kwargs):
            self.assertFalse(os.fspath(path).startswith("/proc"))
            return real_open(path, flags, *args, **kwargs)

        self.assertEqual(
            self.extension_candidates(open_file=guarded_open),
            {"omp-extension": (good_path, metadata)},
        )

    def test_extension_reconnect_has_stable_id_updates_registry_and_projects_capabilities(self):
        manager = self.manager()
        session_id, first_path, first = self.connect_extension(manager, connection="one")
        public = self.model.get(session_id)
        self.assertEqual(public["id"], session_id)
        self.assertEqual(public["ompSessionId"], "omp-extension")
        self.assertEqual(public["title"], "Extension title")
        self.assertEqual(public["transport"]["status"], "connected")
        self.assertEqual(public["subagents"], [{"id": "agent-1", "status": "running"}])
        self.assertTrue(public["capabilities"]["followUp"])
        self.assertIn(("sync_history",), first.calls)
        record = manager.registry.load()[0]
        self.assertEqual(
            (record["id"], record["adapterKind"], record["ompSessionId"]), (session_id, "extension", "omp-extension")
        )

        first_path.unlink()
        Path(str(first_path) + ".json").unlink()
        second_path, _, metadata = self.unix_registration(connection="two", title="Renamed")
        with patch("omp_deck.terminal_sessions.ExtensionAdapter", FakeExtensionAdapter):
            manager._reconcile_extensions({"omp-extension": (second_path, metadata)})
        self.assertTrue(first.closed)
        self.assertEqual(self.model.get(session_id)["id"], session_id)
        self.assertEqual(manager.registry.load()[0]["title"], "Renamed")

    def test_history_disconnect_reconnects_once_as_verified_control_only_session(self):
        manager = self.manager()
        FakeExtensionAdapter.sync_errors.append(ExtensionDisconnected("outbound queue exceeded"))
        path, _, metadata = self.unix_registration()

        with patch("omp_deck.terminal_sessions.ExtensionAdapter", FakeExtensionAdapter):
            manager._reconcile_extensions({"omp-extension": (path, metadata)})
            manager._reconcile_extensions({"omp-extension": (path, metadata)})

        session_id = _terminal_id("omp-extension")
        session = self.model.get(session_id)
        self.assertEqual(len(FakeExtensionAdapter.instances), 2)
        self.assertTrue(FakeExtensionAdapter.instances[0].closed)
        self.assertIn(("sync_history",), FakeExtensionAdapter.instances[0].calls)
        self.assertNotIn(("sync_history",), FakeExtensionAdapter.instances[1].calls)
        self.assertEqual(session["lifecycle"], "ongoing")
        self.assertEqual(session["transport"]["status"], "connected")
        self.assertIsNotNone(session["historyError"])

        manager.command(session_id, "still-live", "sendMessage", {"message": "still live"})
        self.assertIn(("send_message", "still live", None), FakeExtensionAdapter.instances[1].calls)
        FakeExtensionAdapter.instances[1].callback(
            {
                "sequence": 2,
                "kind": "history",
                "data": {"syncId": "later", "offset": 0, "messages": [], "complete": True},
            }
        )
        self.assertIsNone(self.model.get(session_id)["historyError"])

    def test_extension_failure_survives_agent_end_and_idle_refresh(self):
        manager = self.manager()
        session_id, _, adapter = self.connect_extension(manager)
        adapter.callback(
            {
                "sequence": 2,
                "kind": "message",
                "data": {
                    "phase": "end",
                    "message": {
                        "role": "assistant",
                        "content": [],
                        "stopReason": "error",
                        "errorMessage": "Provider unavailable",
                    },
                },
            }
        )
        adapter.callback({"sequence": 3, "kind": "lifecycle", "data": {"state": "agent_end"}})
        adapter._snapshot = ExtensionSnapshot(True, False, None, (), 3, "connected", None)
        manager.refresh_connected()

        failed = self.model.get(session_id)
        self.assertEqual((failed["state"], failed["workingOn"]), ("failed", None))
        self.assertEqual(failed["messages"][-1]["content"], [{"type": "text", "text": "Provider unavailable"}])
        adapter.callback({"sequence": 4, "kind": "lifecycle", "data": {"state": "agent_start"}})
        self.assertEqual(self.model.get(session_id)["state"], "working")

    def test_extension_degraded_event_does_not_fabricate_network_failure(self):
        manager = self.manager()
        session_id, _, adapter = self.connect_extension(manager)

        adapter.callback(
            {
                "sequence": 2,
                "kind": "degraded",
                "data": {
                    "droppedKind": "message",
                    "reason": "event exceeds wire limit",
                },
            }
        )

        session = self.model.get(session_id)
        self.assertEqual(session["state"], "degraded")
        self.assertEqual(session["transport"]["status"], "connected")
        self.assertIsNone(session["transport"]["reason"])

    def test_extension_history_replaces_messages_only_after_complete_sync(self):
        manager = self.manager()
        session_id, _, adapter = self.connect_extension(manager)
        adapter.callback(
            {
                "sequence": 2,
                "kind": "history",
                "data": {
                    "syncId": "initial",
                    "offset": 0,
                    "messages": [{"role": "user", "content": "old branch"}],
                    "complete": True,
                },
            }
        )
        self.assertEqual(self.model.get(session_id)["messages"][0]["content"][0]["text"], "old branch")

        adapter.callback(
            {
                "sequence": 3,
                "kind": "history",
                "data": {
                    "syncId": "tree-selection",
                    "offset": 0,
                    "messages": [{"role": "user", "content": "selected question"}],
                    "complete": False,
                },
            }
        )
        self.assertEqual(self.model.get(session_id)["messages"][0]["content"][0]["text"], "old branch")
        adapter.callback(
            {
                "sequence": 4,
                "kind": "history",
                "data": {
                    "syncId": "tree-selection",
                    "offset": 1,
                    "messages": [{"role": "assistant", "content": "selected answer"}],
                    "complete": True,
                },
            }
        )

        messages = self.model.get(session_id)["messages"]
        self.assertEqual(
            [message["content"][0]["text"] for message in messages],
            ["selected question", "selected answer"],
        )

    @unittest.skipIf(os.name == "nt", "Unix socket transport regression")
    def test_extension_live_message_and_image_survive_later_event_and_history(self):
        manager = self.manager()
        omp_session_id = "omp-real"
        connection_id = "real-connection"
        path = self.settings.socket_root / unix_endpoint_name(omp_session_id, connection_id)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(path))
        listener.listen()
        self.addCleanup(listener.close)
        metadata = {
            "protocol": "omp-deck-fallback-v1",
            "version": 1,
            "type": "registration",
            "sessionId": omp_session_id,
            "connectionId": connection_id,
            "project": "/work/project",
            "title": "Real extension",
        }
        metadata_path = Path(str(path) + ".json")
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        metadata_path.chmod(0o600)
        image = "a" * 64
        send_live = threading.Event()
        history_sent = threading.Event()
        send_history = threading.Event()

        def serve():
            client, _ = listener.accept()
            stream = client.makefile("rb")
            registration_frame = {
                "protocol": "omp-deck-fallback-v1",
                "version": 1,
                "type": "registration",
                "ompVersion": "9.8.7",
                "compatible": True,
                "connectionId": connection_id,
                "session": {"id": omp_session_id, "file": None, "title": "Real extension"},
                "sessionId": omp_session_id,
                "title": "Real extension",
                "project": "/work/project",
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
            client.sendall(extension_line(registration_frame) + extension_line(extension_event(1)))
            request = json.loads(stream.readline())
            client.sendall(extension_line(extension_response(request)))
            send_live.wait(1)
            client.sendall(
                extension_line(
                    extension_event(
                        2,
                        "message",
                        {
                            "phase": "end",
                            "message": {
                                "id": "pictured",
                                "role": "assistant",
                                "content": [{"type": "image", "data": f"blob:sha256:{image}"}],
                            },
                        },
                    )
                )
            )
            client.sendall(
                extension_line(
                    extension_event(
                        3,
                        "message",
                        {
                            "phase": "end",
                            "message": {"id": "later", "role": "assistant", "content": "Next message"},
                        },
                    )
                )
            )
            send_history.wait(1)
            client.sendall(
                extension_line(
                    extension_event(
                        4,
                        "history",
                        {
                            "syncId": "replacement",
                            "offset": 0,
                            "messages": [
                                {
                                    "id": "history-image",
                                    "role": "assistant",
                                    "content": [{"type": "image", "url": f"/api/blob/{image}"}],
                                }
                            ],
                            "complete": True,
                        },
                    )
                )
            )
            history_sent.set()
            threading.Event().wait(0.05)
            stream.close()
            client.close()

        server = threading.Thread(target=serve)
        server.start()
        self.addCleanup(server.join, 1)
        manager._reconcile_extensions({omp_session_id: (path, metadata)})
        session_id = _terminal_id(omp_session_id)
        send_live.set()
        for _ in range(200):
            messages = self.model.get(session_id)["messages"]
            if len(messages) == 2:
                break
            threading.Event().wait(0.005)
        self.assertEqual([message["id"] for message in messages], ["pictured", "later"])
        self.assertEqual(messages[0]["content"], [{"type": "image", "url": f"/api/blob/{image}"}])
        send_history.set()
        self.assertTrue(history_sent.wait(1))
        for _ in range(200):
            messages = self.model.get(session_id)["messages"]
            if [message["id"] for message in messages] == ["history-image"]:
                break
            threading.Event().wait(0.005)
        self.assertEqual([message["id"] for message in messages], ["history-image"])
        self.assertEqual(messages[0]["content"], [{"type": "image", "url": f"/api/blob/{image}"}])

    @unittest.skipIf(os.name == "nt", "Unix socket activation race regression")
    def test_extension_activation_serializes_staged_history_with_live_reader_event(self):
        manager = self.manager()
        omp_session_id = "omp-activation-race"
        connection_id = "activation-connection"
        path = self.settings.socket_root / unix_endpoint_name(omp_session_id, connection_id)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(path))
        listener.listen()
        self.addCleanup(listener.close)
        metadata = {
            "protocol": "omp-deck-fallback-v1",
            "version": 1,
            "type": "registration",
            "sessionId": omp_session_id,
            "connectionId": connection_id,
            "project": "/work/project",
            "title": "Activation race",
        }
        drained = threading.Event()
        send_live = threading.Event()
        callback_completed = threading.Event()
        final_completed = threading.Event()
        send_final = threading.Event()
        finish_server = threading.Event()
        callback_attempted = threading.Event()
        release_drain = threading.Event()

        def serve():
            client, _ = listener.accept()
            stream = client.makefile("rb")
            registration = {
                "protocol": "omp-deck-fallback-v1",
                "version": 1,
                "type": "registration",
                "ompVersion": "9.8.7",
                "compatible": True,
                "connectionId": connection_id,
                "session": {"id": omp_session_id, "file": None, "title": "Activation race"},
                "sessionId": omp_session_id,
                "title": "Activation race",
                "project": "/work/project",
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
            client.sendall(extension_line(registration) + extension_line(extension_event(1)))
            request = json.loads(stream.readline())
            for sequence, role, text in ((2, "user", "question"), (3, "assistant", "answer")):
                client.sendall(
                    extension_line(
                        extension_event(
                            sequence,
                            "history",
                            {
                                "syncId": "initial",
                                "offset": sequence - 2,
                                "messages": [{"id": str(sequence), "role": role, "content": text}],
                                "complete": False,
                            },
                        )
                    )
                )
            client.sendall(
                extension_line(
                    extension_event(
                        4,
                        "history",
                        {"syncId": "initial", "offset": 2, "messages": [], "complete": True},
                    )
                )
                + extension_line(extension_response(request))
            )
            send_live.wait(1)
            client.sendall(
                extension_line(
                    extension_event(
                        5,
                        "message",
                        {
                            "phase": "end",
                            "message": {"id": "5", "role": "assistant", "content": "later"},
                        },
                    )
                )
            )
            send_final.wait(2)
            client.sendall(
                extension_line(
                    extension_event(
                        6,
                        "message",
                        {
                            "phase": "end",
                            "message": {"id": "6", "role": "assistant", "content": "final"},
                        },
                    )
                )
            )
            finish_server.wait(2)
            stream.close()
            client.close()

        server = threading.Thread(target=serve)
        server.start()
        self.addCleanup(server.join, 1)
        for signal in (finish_server, send_final, send_live, release_drain):
            self.addCleanup(signal.set)
        original_drain = RealExtensionAdapter.drain_events

        def observed_adapter(endpoint, **options):
            publish = options["on_event"]

            def on_event(event):
                sequence = event.get("sequence")
                if sequence == 5:
                    callback_attempted.set()
                publish(event)
                if sequence == 5:
                    callback_completed.set()
                elif sequence == 6:
                    final_completed.set()

            options["on_event"] = on_event
            return RealExtensionAdapter(endpoint, **options)

        def paused_drain(adapter):
            events = original_drain(adapter)
            drained.set()
            send_live.set()
            self.assertTrue(callback_attempted.wait(1))
            release_drain.wait(1)
            return events

        connect = threading.Thread(target=lambda: manager._reconcile_extensions({omp_session_id: (path, metadata)}))
        with (
            patch.object(RealExtensionAdapter, "drain_events", paused_drain),
            patch("omp_deck.terminal_sessions.ExtensionAdapter", observed_adapter),
        ):
            connect.start()
            self.assertTrue(drained.wait(1))
            self.assertTrue(callback_attempted.wait(1))
            release_drain.set()
            connect.join(1)
        self.assertFalse(connect.is_alive())
        self.assertTrue(callback_completed.wait(1))
        send_final.set()
        self.assertTrue(final_completed.wait(1))
        session = self.model.get(_terminal_id(omp_session_id))
        self.assertEqual(
            [message["content"][0]["text"] for message in session["messages"]],
            ["question", "answer", "later", "final"],
        )
        self.assertEqual(session["transport"]["status"], "connected")

    @unittest.skipIf(os.name == "nt", "Unix socket ordering regression")
    def test_newer_dead_endpoint_falls_back_without_replacing_live_older_runtime(self):
        manager = self.manager()
        old_path, old_metadata_path, old_metadata = self.unix_registration(connection="old")
        candidates = self.extension_candidates()
        with patch("omp_deck.terminal_sessions.ExtensionAdapter", FakeExtensionAdapter):
            manager._reconcile_extensions(candidates)
        session_id = _terminal_id("omp-extension")
        old_runtime = manager._runtimes.get(session_id)
        self.assertIsNotNone(old_runtime)

        dead_connection = "dead"
        dead_path = self.settings.socket_root / unix_endpoint_name("omp-extension", dead_connection)
        dead_listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        dead_listener.bind(str(dead_path))
        dead_listener.close()
        dead_metadata = dict(old_metadata, connectionId=dead_connection)
        dead_metadata_path = Path(str(dead_path) + ".json")
        dead_metadata_path.write_text(json.dumps(dead_metadata), encoding="utf-8")
        dead_metadata_path.chmod(0o600)
        newer = old_metadata_path.stat().st_mtime_ns + 1_000_000
        os.utime(dead_metadata_path, ns=(newer, newer))

        attempted = threading.Event()

        def adapter_for(endpoint, **options):
            if endpoint == str(dead_path):
                attempted.set()
                return RealExtensionAdapter(endpoint, **options)
            return FakeExtensionAdapter(endpoint, **options)

        with patch("omp_deck.terminal_sessions.ExtensionAdapter", adapter_for):
            manager.start()
            self.assertTrue(attempted.wait(2), "discovery never tried the newer endpoint")
            # The factory runs under the command lock. Acquire it only after
            # construction begins, so assertions observe completed initialization.
            with manager._commands.lock(session_id):
                self.assertIs(manager._runtimes.get(session_id), old_runtime)
        self.assertIs(manager._runtimes.get(session_id), old_runtime)
        self.assertFalse(old_runtime.adapter.closed)

    @unittest.skipIf(os.name == "nt", "Unix socket ordering regression")
    def test_cold_discovery_falls_back_from_newer_dead_endpoint(self):
        manager = self.manager(discovery_interval=0.01)
        old_path, old_metadata_path, old_metadata = self.unix_registration(connection="old")
        dead_path = self.settings.socket_root / unix_endpoint_name("omp-extension", "dead")
        dead_listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        dead_listener.bind(str(dead_path))
        dead_listener.close()
        dead_metadata = dict(old_metadata, connectionId="dead")
        dead_metadata_path = Path(str(dead_path) + ".json")
        dead_metadata_path.write_text(json.dumps(dead_metadata), encoding="utf-8")
        dead_metadata_path.chmod(0o600)
        newer = old_metadata_path.stat().st_mtime_ns + 1_000_000
        os.utime(dead_metadata_path, ns=(newer, newer))

        def adapter_for(endpoint, **options):
            if endpoint == str(dead_path):
                return RealExtensionAdapter(endpoint, **options)
            return FakeExtensionAdapter(endpoint, **options)

        session_id = _terminal_id("omp-extension")
        with patch("omp_deck.terminal_sessions.ExtensionAdapter", adapter_for):
            manager.start()
            for _ in range(200):
                runtime = manager._runtimes.get(session_id)
                if runtime is not None:
                    break
                threading.Event().wait(0.005)
        self.assertIsNotNone(runtime)
        self.assertEqual(runtime.socket_path, old_path)
        self.assertEqual(self.model.get(session_id)["transport"]["status"], "connected")

    def test_expired_socket_or_heartbeat_drops_runtime_but_persists_disconnected_session(self):
        manager = self.manager()
        session_id, path, adapter = self.connect_extension(manager)
        path.unlink()
        Path(str(path) + ".json").unlink()
        manager._reconcile_extensions({})
        self.assertTrue(adapter.closed)
        self.assertEqual(self.model.get(session_id)["state"], "disconnected")
        self.assertEqual(manager.registry.load()[0]["id"], session_id)

        path2, _, metadata = self.unix_registration(connection="new")
        with patch("omp_deck.terminal_sessions.ExtensionAdapter", FakeExtensionAdapter):
            manager._reconcile_extensions({"omp-extension": (path2, metadata)})
        current = FakeExtensionAdapter.instances[-1]
        current._snapshot = ExtensionSnapshot(True, False, None, (), 2, "degraded", "heartbeat expired")
        with patch("omp_deck.terminal_sessions.ExtensionAdapter", FakeExtensionAdapter):
            manager._reconcile_extensions({"omp-extension": (path2, metadata)})
        self.assertTrue(current.closed)
        self.assertEqual(self.model.get(session_id)["id"], session_id)

    def test_removed_extension_stays_hidden_across_manager_restart(self):
        manager = self.manager()
        path, _, metadata = self.unix_registration(connection="failed")
        with patch("omp_deck.terminal_sessions.ExtensionAdapter", FakeExtensionAdapter):
            manager._reconcile_extensions({"omp-extension": (path, metadata)})

        record = manager.registry.load()[0]
        session_id = record["id"]
        self.assertIsNotNone(manager._runtimes.get(session_id))
        self.assertEqual(record["recoveryMetadata"], {"connectionId": "failed"})
        self.assertTrue(manager.remove(session_id))

        tombstone = manager.registry.load()[0]
        self.assertEqual(
            tombstone["recoveryMetadata"],
            {"connectionId": "failed", "removed": True},
        )
        manager.close()
        self.managers.remove(manager)
        restarted = self.manager()

        with patch("omp_deck.terminal_sessions.ExtensionAdapter", FakeExtensionAdapter):
            restarted._reconcile_extensions({"omp-extension": (path, metadata)})
        self.assertIsNone(restarted._runtimes.get(session_id))
        self.assertEqual(restarted.registry.load(), [tombstone])

        new_path, _, new_metadata = self.unix_registration(connection="new")
        with patch("omp_deck.terminal_sessions.ExtensionAdapter", FakeExtensionAdapter):
            restarted._reconcile_extensions({"omp-extension": (new_path, new_metadata)})
        current = restarted.registry.load()[0]
        self.assertEqual(current["id"], session_id)
        self.assertEqual(current["recoveryMetadata"], {"connectionId": "new"})
        self.assertIsNotNone(restarted._runtimes.get(session_id))

    def test_failed_replacement_history_sync_keeps_existing_runtime_operable(self):
        manager = self.manager()
        session_id, _, existing = self.connect_extension(manager, connection="old")
        path, _, metadata = self.unix_registration(connection="failed")
        FakeExtensionAdapter.fail_sync = True

        with patch("omp_deck.terminal_sessions.ExtensionAdapter", FakeExtensionAdapter):
            manager._reconcile_extensions({"omp-extension": (path, metadata)})

        self.assertIs(manager._runtimes.get(session_id).adapter, existing)
        self.assertFalse(existing.closed)
        self.assertEqual(self.model.get(session_id)["transport"]["status"], "connected")
        manager.command(session_id, "still-live", "shutdown", {})
        self.assertIn(("shutdown_session",), existing.calls)
        FakeExtensionAdapter.fail_sync = False

    @unittest.skipIf(os.name == "nt", "Unix socket ordering regression")
    def test_removed_connection_tombstone_survives_newer_dead_candidate_and_repeated_scans(self):
        manager = self.manager(discovery_interval=0.01)
        path, metadata_path, metadata = self.unix_registration(connection="removed")
        with patch("omp_deck.terminal_sessions.ExtensionAdapter", FakeExtensionAdapter):
            manager._reconcile_extensions({"omp-extension": (path, metadata)})
        session_id = _terminal_id("omp-extension")
        self.assertTrue(manager.remove(session_id))
        tombstone = manager.registry.load()[0]
        manager.close()
        self.managers.remove(manager)

        dead_path = self.settings.socket_root / unix_endpoint_name("omp-extension", "dead")
        dead_listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        dead_listener.bind(str(dead_path))
        dead_listener.close()
        dead_metadata = dict(metadata, connectionId="dead")
        dead_metadata_path = Path(str(dead_path) + ".json")
        dead_metadata_path.write_text(json.dumps(dead_metadata), encoding="utf-8")
        dead_metadata_path.chmod(0o600)
        newer = metadata_path.stat().st_mtime_ns + 1_000_000
        os.utime(dead_metadata_path, ns=(newer, newer))

        restarted = self.manager(discovery_interval=0.01)
        restarted.start()
        threading.Event().wait(0.06)
        self.assertIsNone(restarted._runtimes.get(session_id))
        self.assertEqual(restarted.registry.load(), [tombstone])

    def test_collab_credential_is_secret_commands_route_and_remove_forgets_everything(self):
        secret = collab_link()
        manager = self.manager()
        with patch("omp_deck.terminal_sessions.CollabAdapter", FakeCollabAdapter):
            public = manager.add_collab(secret, title="Private")
        session_id = public["id"]
        adapter = FakeCollabAdapter.instances[-1]
        self.assertNotIn(secret, repr(public))
        self.assertNotIn(secret, repr(self.model.snapshot()))
        self.assertNotIn(secret, repr(manager.registry.load()))

        manager.command(session_id, "m", "sendMessage", {"message": "hello", "images": [{"data": "AA"}]})
        manager.command(session_id, "i", "interrupt", {})
        manager.command(session_id, "u", "respondToUi", {"requestId": "7", "value": {"ok": True}})
        manager.command(session_id, "c", "cancelUi", {"requestId": 8})
        self.assertIn(("send_message", "hello", [{"data": "AA"}]), adapter.calls)
        self.assertIn(("interrupt",), adapter.calls)
        self.assertIn(("respond_ui", 7, '{"ok":true}'), adapter.calls)
        self.assertIn(("respond_ui", 8, None), adapter.calls)

        self.assertTrue(manager.remove(session_id))
        self.assertIsNone(manager.credentials.get_link(session_id))
        self.assertEqual(manager.registry.load(), [])
        with self.assertRaises(KeyError):
            self.model.get(session_id)

    def test_remove_refuses_rpc_session_owned_by_rpc_manager(self):
        registry = RegistryStore(self.settings.state_root / "registry.json")
        record = registry.create(
            adapter_kind="rpc",
            project_name="project",
            project_path=str(self.settings.project_root),
            title="RPC",
            desired_rpc_running=True,
        )
        self.model.load_record(record)
        manager = self.manager(registry=registry)

        self.assertFalse(manager.remove(record["id"]))
        self.assertEqual(registry.load(), [record])
        self.assertEqual(self.model.get(record["id"])["id"], record["id"])

    def test_collab_replacement_close_failure_keeps_the_live_runtime_owned(self):
        manager = self.manager()
        with patch("omp_deck.terminal_sessions.CollabAdapter", FakeCollabAdapter):
            session_id = manager.add_collab(collab_link())["id"]
            original = manager._runtimes.get(session_id)
            record = manager.registry.load()[0]
            FakeCollabAdapter.fail_close = True
            try:
                with self.assertRaisesRegex(RuntimeError, "close failed"):
                    manager._connect_collab(record, collab_link())
            finally:
                FakeCollabAdapter.fail_close = False

        self.assertIs(manager._runtimes.get(session_id), original)
        self.assertNotIn(("start",), FakeCollabAdapter.instances[-1].calls)

    def test_collab_connection_failure_rolls_back_every_owned_resource(self):
        manager = self.manager()
        FakeCollabAdapter.fail_start = True

        with patch("omp_deck.terminal_sessions.CollabAdapter", FakeCollabAdapter):
            with self.assertRaisesRegex(CollabImportError, "connection unavailable"):
                manager.add_collab(collab_link())

        self.assertEqual(self.model.snapshot()["sessions"], [])
        self.assertEqual(manager.credentials.list(), [])
        self.assertEqual(manager.registry.load(), [])
        self.assertTrue(FakeCollabAdapter.instances[-1].closed)

    def test_collab_failed_cleanup_retains_every_resource_until_remove_succeeds(self):
        manager = self.manager()
        FakeCollabAdapter.fail_start = True
        FakeCollabAdapter.fail_close = True

        with patch("omp_deck.terminal_sessions.CollabAdapter", FakeCollabAdapter):
            with self.assertRaisesRegex(CollabImportError, "rollback failed"):
                manager.add_collab(collab_link())

        record = manager.registry.load()[0]
        self.assertIsNotNone(manager._runtimes.get(record["id"]))
        self.assertIsNotNone(manager.credentials.get_link(record["id"]))
        self.assertEqual(self.model.get(record["id"])["id"], record["id"])

        FakeCollabAdapter.fail_close = False
        self.assertTrue(manager.remove(record["id"]))
        self.assertIsNone(manager._runtimes.get(record["id"]))
        self.assertEqual(manager.registry.load(), [])

    def test_collab_import_request_is_durable_and_does_not_store_secret(self):
        manager = self.manager()
        secret = collab_link()
        with patch("omp_deck.terminal_sessions.CollabAdapter", FakeCollabAdapter):
            first = manager.add_collab_request("collab-import-1", secret, title="Private")
            duplicate = manager.add_collab_request("collab-import-1", secret, title="Private")

        self.assertEqual(duplicate["session"]["id"], first["session"]["id"])
        self.assertEqual(len(FakeCollabAdapter.instances), 1)
        self.assertNotIn(
            secret,
            (self.settings.state_root / "requests.json").read_text(),
        )
        with self.assertRaises(TerminalCommandConflict):
            manager.add_collab_request("collab-import-1", secret, title="Different")

    def test_invalid_collab_import_does_not_reserve_request(self):
        manager = self.manager()

        with self.assertRaisesRegex(ValueError, "invalid collab link"):
            manager.add_collab_request("invalid-import", "not a collab link")

        self.assertFalse((self.settings.state_root / "requests.json").exists())

    def test_real_read_only_collab_adapter_rejects_mutation_before_socket_write(self):
        frames = [host({**WELCOME, "readOnly": True}), host(EMPTY_CHUNK)]
        sock = CollabSocket(frames)
        adapter = CollabAdapter(collab_link(False), "viewer", connector=lambda url, **options: sock)
        self.addCleanup(adapter.close)
        adapter.start()
        sent = len(sock.sent)
        for operation in (
            lambda: adapter.send_message("no"),
            adapter.interrupt,
            lambda: adapter.respond_ui(3, "no"),
        ):
            with self.subTest(operation=operation):
                with self.assertRaises(AdapterError):
                    operation()
        self.assertEqual(len(sock.sent), sent)
        self.assertNotIn(collab_link(False), repr(adapter.snapshot()))
        for frame in sock.sent:
            _, sealed = unpack_envelope(frame)
            self.assertNotIn(collab_link(False), repr(open_frame(KEY, sealed, direction="guest")))

    def test_request_id_deduplicates_and_conflicting_reuse_is_rejected(self):
        manager = self.manager()
        with patch("omp_deck.terminal_sessions.CollabAdapter", FakeCollabAdapter):
            session_id = manager.add_collab(collab_link())["id"]
        adapter = FakeCollabAdapter.instances[-1]
        first = manager.command(session_id, "same", "sendMessage", {"message": "once"})
        second = manager.command(session_id, "same", "sendMessage", {"message": "once"})
        self.assertEqual(first, second)
        self.assertEqual(adapter.calls.count(("send_message", "once", None)), 1)
        with self.assertRaises(TerminalCommandConflict):
            manager.command(session_id, "same", "sendMessage", {"message": "different"})

    def test_completed_terminal_request_is_not_replayed_after_manager_restart(self):
        manager = self.manager()
        with patch("omp_deck.terminal_sessions.CollabAdapter", FakeCollabAdapter):
            session_id = manager.add_collab(collab_link())["id"]
        first_adapter = FakeCollabAdapter.instances[-1]
        first = manager.command(session_id, "durable-terminal", "sendMessage", {"message": "once"})
        manager.close()
        restarted = self.manager()
        with patch("omp_deck.terminal_sessions.CollabAdapter", FakeCollabAdapter):
            restarted.start()
            deadline = time.monotonic() + 1
            while restarted._runtimes.get(session_id) is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertIsNotNone(restarted._runtimes.get(session_id))

        duplicate = restarted.command(session_id, "durable-terminal", "sendMessage", {"message": "once"})

        self.assertEqual(duplicate, first)
        self.assertEqual(first_adapter.calls.count(("send_message", "once", None)), 1)
        self.assertNotIn(("send_message", "once", None), FakeCollabAdapter.instances[-1].calls)

    def test_thinking_refresh_does_not_replay_stale_cached_idle_state(self):
        manager = self.manager()
        session_id, _, adapter = self.connect_extension(manager)
        adapter.callback({"sequence": 2, "kind": "lifecycle", "data": {"state": "agent_start"}})
        self.assertTrue(adapter.snapshot().idle)
        self.assertEqual(self.model.get(session_id)["state"], "working")
        adapter._refresh_snapshot = replace(adapter.snapshot(), idle=False, sequence=2)

        manager.command(session_id, "thinking-running", "setThinking", {"level": "low"})

        self.assertIn(("refresh",), adapter.calls)
        self.assertEqual(self.model.get(session_id)["state"], "working")
        self.assertEqual(self.model.get(session_id)["thinking"], "low")

    def test_extension_commands_route_and_unadvertised_capability_is_rejected(self):
        manager = self.manager()
        session_id, _, adapter = self.connect_extension(manager)
        manager.command(session_id, "send", "sendMessage", {"message": "now"})
        manager.command(session_id, "follow", "followUp", {"message": "later"})
        manager.command(session_id, "interrupt", "interrupt", {})
        manager.command(session_id, "shutdown", "shutdown", {})
        first_thinking = manager.command(session_id, "thinking", "setThinking", {"level": "low"})
        self.assertEqual(
            manager.command(session_id, "thinking", "setThinking", {"level": "low"}),
            first_thinking,
        )
        with self.assertRaises(TerminalCommandConflict):
            manager.command(session_id, "thinking", "setThinking", {"level": "high"})
        self.assertIn(("send_message", "now", None), adapter.calls)
        self.assertIn(("send_message", "later", "followUp"), adapter.calls)
        self.assertIn(("interrupt",), adapter.calls)
        self.assertIn(("shutdown_session",), adapter.calls)
        self.assertEqual(adapter.calls.count(("set_thinking", "low")), 1)
        self.assertEqual(self.model.get(session_id)["thinking"], "low")
        adapter.callback(
            {
                "sequence": 2,
                "kind": "heartbeat",
                "data": {
                    "time": 1,
                    "thinking": "xhigh",
                    "thinkingLevels": ["off", "low", "high", "xhigh"],
                },
            }
        )
        self.assertEqual(self.model.get(session_id)["thinking"], "xhigh")
        self.assertEqual(self.model.get(session_id)["thinkingLevels"], ["off", "low", "high", "xhigh"])

        adapter.registration = ExtensionRegistration(
            1,
            "9",
            "omp-extension",
            "/work/project",
            "x",
            "connection-1",
            MappingProxyType({"sendMessage": False, "followUp": False, "interrupt": False, "shutdown": False}),
        )
        adapter._snapshot = ExtensionSnapshot(True, False, None, (), 2, "connected", None)
        manager.refresh_connected()
        legacy = self.model.get(session_id)
        self.assertIsNone(legacy["thinking"])
        self.assertEqual(legacy["thinkingLevels"], [])
        self.assertFalse(legacy["capabilities"]["setThinking"])
        for index, action in enumerate(("sendMessage", "followUp", "interrupt", "shutdown", "setThinking")):
            payload = (
                {"message": "x"}
                if action in {"sendMessage", "followUp"}
                else {"level": "low"}
                if action == "setThinking"
                else {}
            )
            with self.subTest(action=action):
                with self.assertRaises(ValueError):
                    manager.command(session_id, f"unsupported-{index}", action, payload)

    def test_callbacks_and_refresh_normalize_messages_subagents_and_ui(self):
        manager = self.manager()
        with patch("omp_deck.terminal_sessions.CollabAdapter", FakeCollabAdapter):
            session_id = manager.add_collab(collab_link())["id"]
        adapter = FakeCollabAdapter.instances[-1]
        adapter._snapshot = CollabSnapshot(
            header=MappingProxyType({"id": "omp-collab"}),
            state=MappingProxyType({"cwd": "/work/collab", "isStreaming": False}),
            entries=(
                MappingProxyType(
                    {
                        "type": "message",
                        "id": "entry",
                        "timestamp": "now",
                        "message": {"role": "assistant", "content": "answer"},
                    }
                ),
                MappingProxyType(
                    {
                        "type": "custom_message",
                        "customType": "collab-prompt",
                        "display": True,
                        "id": "prompt",
                        "timestamp": "now",
                        "content": "optimistic prompt",
                    }
                ),
            ),
            agents=(
                MappingProxyType({"id": "main", "kind": "main", "status": "running"}),
                MappingProxyType({"id": "agent", "kind": "sub", "status": "running"}),
            ),
            pending_ui_requests=(
                MappingProxyType(
                    {
                        "reqId": 12,
                        "kind": "select",
                        "title": "Proceed?",
                        "options": ["Yes", "No"],
                        "selectionMarker": "checkbox",
                        "checkedIndices": [0],
                    }
                ),
            ),
            sequence=2,
            transport_status="connected",
            reason=None,
            read_only=False,
        )
        adapter._callback(AdapterEvent(2, "entry", {}, 2.0))
        public = self.model.get(session_id)
        self.assertEqual(public["messages"][0]["content"], [{"type": "text", "text": "answer"}])
        self.assertEqual(public["messages"][1]["role"], "user")
        self.assertEqual(
            public["messages"][1]["content"],
            [{"type": "text", "text": "optimistic prompt"}],
        )
        self.assertEqual([agent["id"] for agent in public["subagents"]], ["agent"])
        self.assertFalse(public["capabilities"]["todos"])
        self.assertTrue(public["capabilities"]["remove"])
        self.assertEqual(public["pendingUiRequest"]["id"], "12")
        self.assertTrue(public["pendingUiRequest"]["questions"][0]["multi"])
        self.assertEqual(
            public["pendingUiRequest"]["questions"][0]["checkedIndices"],
            [0],
        )
        self.assertEqual(public["state"], "needs_input")

    def test_background_recovery_serves_fast_collab_while_slow_peer_waits_and_close_cancels_it(self):
        slow_link = collab_link()
        fast_link = slow_link.replace(ROOM, "klmnopqrst")
        registry = RegistryStore(self.settings.state_root / "registry.json")
        credentials = CollabStore(self.settings.state_root)
        slow = registry.create(
            session_id="slow-collab",
            adapter_kind="collab",
            project_name="Terminal",
            project_path=str(self.settings.project_root),
            title="Slow",
        )
        fast = registry.create(
            session_id="fast-collab",
            adapter_kind="collab",
            project_name="Terminal",
            project_path=str(self.settings.project_root),
            title="Fast",
        )
        credentials.put(slow["id"], slow_link)
        credentials.put(fast["id"], fast_link)
        manager = self.manager(registry=registry, credentials=credentials)
        slow_started = threading.Event()
        release_slow = threading.Event()

        class RecoveringCollabAdapter(FakeCollabAdapter):
            def start(self):
                if self._link_for_assertion == slow_link:
                    self.calls.append(("start",))
                    slow_started.set()
                    release_slow.wait(2)
                    if self.closed:
                        raise RuntimeError("closed during startup")
                    return
                super().start()

            def close(self):
                super().close()
                release_slow.set()

        with patch("omp_deck.terminal_sessions.CollabAdapter", RecoveringCollabAdapter):
            manager.start()
            self.assertTrue(slow_started.wait(1))
            deadline = time.monotonic() + 1
            while self.model.get(fast["id"])["transport"]["status"] != "connected" and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(self.model.get(fast["id"])["transport"]["status"], "connected")
            self.assertEqual(self.model.get(slow["id"])["transport"]["status"], "reconnecting")

            manager.close(timeout=1)
        self.assertIsNone(manager._runtimes.get(slow["id"]))
        self.assertIsNone(manager._runtimes.get(fast["id"]))
        self.assertEqual(self.model.get(slow["id"])["transport"]["status"], "disconnected")

    def test_removal_wins_over_queued_collab_recovery(self):
        registry = RegistryStore(self.settings.state_root / "registry.json")
        credentials = CollabStore(self.settings.state_root)
        record = registry.create(
            session_id="removed-collab",
            adapter_kind="collab",
            project_name="Terminal",
            project_path=str(self.settings.project_root),
            title="Removed",
        )
        credentials.put(record["id"], collab_link())
        manager = self.manager(registry=registry, credentials=credentials)
        entered, release, connected = (threading.Event() for _ in range(3))

        class QueuedAdapter(FakeCollabAdapter):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                entered.set()
                release.wait(2)

            def start(self):
                connected.set()
                super().start()

        with patch("omp_deck.terminal_sessions.CollabAdapter", QueuedAdapter):
            manager.start()
            try:
                self.assertTrue(entered.wait(1))
                self.assertTrue(manager.remove(record["id"]))
            finally:
                release.set()
            deadline = time.monotonic() + 1
            while manager._collab_recovery._workers and time.monotonic() < deadline:
                time.sleep(0.01)
            manager.close(timeout=1)
        self.assertFalse(connected.is_set())
        self.assertEqual(registry.load(), [])
        with self.assertRaises(KeyError):
            self.model.get(record["id"])

    def test_close_joins_watcher_and_closes_all_adapters(self):
        manager = self.manager()
        manager.start()
        watcher = manager._watcher
        self.assertIsNotNone(watcher)
        session_id, _, extension = self.connect_extension(manager)
        with patch("omp_deck.terminal_sessions.CollabAdapter", FakeCollabAdapter):
            manager.add_collab(collab_link())
        manager.close()
        self.assertFalse(watcher.is_alive())
        self.assertTrue(extension.closed)
        self.assertTrue(all(adapter.closed for adapter in FakeCollabAdapter.instances))
        self.assertEqual(self.model.get(session_id)["state"], "disconnected")
        self.managers.remove(manager)

    def test_close_reports_failed_adapter_and_retains_runtime(self):
        manager = self.manager()
        session_id, _, _ = self.connect_extension(manager)
        FakeExtensionAdapter.fail_close = True
        try:
            with self.assertRaisesRegex(
                RuntimeError,
                "terminal session shutdown failed.*close failed",
            ):
                manager.close(timeout=0.2)
            self.assertIsNotNone(manager._runtimes.get(session_id))
        finally:
            FakeExtensionAdapter.fail_close = False
        manager.close()
        self.assertIsNone(manager._runtimes.get(session_id))
        self.managers.remove(manager)


if __name__ == "__main__":
    unittest.main()
