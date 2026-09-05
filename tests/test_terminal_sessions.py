from __future__ import annotations

import hashlib
import json
import os
import socket
import tempfile
import threading
import unittest
from collections import deque
from pathlib import Path
from types import MappingProxyType
from unittest.mock import patch

from omp_deck._collab_imports import CollabImportError
from omp_deck._terminal_commands import TerminalCommandConflict
from omp_deck._terminal_discovery import discover_extension_candidates
from omp_deck.collab_adapter import CollabAdapter, CollabSnapshot
from omp_deck.collab_protocol import b64url_encode, open_frame, pack_envelope, seal_frame, unpack_envelope
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
    capabilities = {
        "sendMessage": True,
        "followUp": True,
        "interrupt": True,
        "shutdown": True,
        "todos": True,
        "subagents": True,
        "answerUiRequest": False,
    }

    def __init__(self, socket_path, on_event=None):
        self.socket_path = socket_path
        self.callback = on_event
        self.calls = []
        self.closed = False
        connection = Path(socket_path).name.removesuffix(".sock").rsplit(".", 1)[-1]
        self.registration = ExtensionRegistration(
            protocol_version=1,
            omp_version="9.8.7",
            session_id="omp-extension",
            project="/work/project",
            title="Extension title",
            connection_id=connection,
            capabilities=MappingProxyType(dict(self.capabilities)),
        )
        self._snapshot = ExtensionSnapshot(
            idle=True,
            pending_messages=False,
            model=MappingProxyType({"provider": "openai", "id": "gpt"}),
            async_jobs=({"id": "agent-1", "status": "running"},),
            sequence=1,
            transport_status="connected",
            reason=None,
        )
        self.__class__.instances.append(self)

    def start(self):
        self.calls.append(("start",))
        if type(self).fail_start:
            raise RuntimeError("connect failed")
        return self.registration

    def snapshot(self):
        return self._snapshot

    def send_message(self, message, behavior=None):
        self.calls.append(("send_message", message, behavior))

    def interrupt(self):
        self.calls.append(("interrupt",))

    def shutdown_session(self):
        self.calls.append(("shutdown_session",))

    def close(self):
        self.calls.append(("close",))
        if type(self).fail_close:
            raise RuntimeError("close failed")
        self.closed = True


class TerminalSessionManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
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
        socket_id = hashlib.sha256(session.encode()).hexdigest()[:24]
        path = self.settings.socket_root / f"{socket_id}.123.{connection}.sock"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(path))
        self.addCleanup(listener.close)
        metadata = {
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

        wrong_path = self.settings.socket_root / "wrong.123.connection-x.sock"
        wrong = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        wrong.bind(str(wrong_path))
        self.addCleanup(wrong.close)
        Path(str(wrong_path) + ".json").write_text(
            json.dumps({"sessionId": "different", "connectionId": "connection-x"}), encoding="utf-8"
        )
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

    def test_extension_degraded_event_is_visible_in_fleet_state(self):
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
        self.assertEqual(session["transport"]["status"], "degraded")
        self.assertEqual(session["transport"]["reason"], "event exceeds wire limit")

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
        FakeExtensionAdapter.fail_start = True

        with patch("omp_deck.terminal_sessions.ExtensionAdapter", FakeExtensionAdapter):
            manager._reconcile_extensions({"omp-extension": (path, metadata)})

        record = manager.registry.load()[0]
        session_id = record["id"]
        self.assertIsNone(manager._runtimes.get(session_id))
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

        FakeExtensionAdapter.fail_start = False
        with patch("omp_deck.terminal_sessions.ExtensionAdapter", FakeExtensionAdapter):
            restarted._reconcile_extensions({"omp-extension": (path, metadata)})
        self.assertIsNone(restarted._runtimes.get(session_id))
        self.assertEqual(restarted.registry.load(), [tombstone])

        new_path, _, new_metadata = self.unix_registration(connection="new")
        with patch("omp_deck.terminal_sessions.ExtensionAdapter", FakeExtensionAdapter):
            restarted._reconcile_extensions({"omp-extension": (new_path, new_metadata)})
        current = restarted.registry.load()[0]
        self.assertEqual(current["id"], session_id)
        self.assertEqual(
            current["recoveryMetadata"],
            {"connectionId": "new"},
        )
        self.assertIsNotNone(restarted._runtimes.get(session_id))

    def test_extension_failed_cleanup_retains_runtime_until_remove_succeeds(self):
        manager = self.manager()
        path, _, metadata = self.unix_registration(connection="failed")
        FakeExtensionAdapter.fail_start = True
        FakeExtensionAdapter.fail_close = True

        with patch("omp_deck.terminal_sessions.ExtensionAdapter", FakeExtensionAdapter):
            manager._reconcile_extensions({"omp-extension": (path, metadata)})

        record = manager.registry.load()[0]
        self.assertIsNotNone(manager._runtimes.get(record["id"]))
        transport = self.model.get(record["id"])["transport"]
        self.assertEqual(transport["status"], "degraded")
        self.assertIn("cleanup failed", transport["reason"])

        FakeExtensionAdapter.fail_close = False
        self.assertTrue(manager.remove(record["id"]))
        self.assertIsNone(manager._runtimes.get(record["id"]))

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

        duplicate = restarted.command(session_id, "durable-terminal", "sendMessage", {"message": "once"})

        self.assertEqual(duplicate, first)
        self.assertEqual(first_adapter.calls.count(("send_message", "once", None)), 1)
        self.assertNotIn(("send_message", "once", None), FakeCollabAdapter.instances[-1].calls)

    def test_extension_commands_route_and_unadvertised_capability_is_rejected(self):
        manager = self.manager()
        session_id, _, adapter = self.connect_extension(manager)
        manager.command(session_id, "send", "sendMessage", {"message": "now"})
        manager.command(session_id, "follow", "followUp", {"message": "later"})
        manager.command(session_id, "interrupt", "interrupt", {})
        manager.command(session_id, "shutdown", "shutdown", {})
        self.assertIn(("send_message", "now", None), adapter.calls)
        self.assertIn(("send_message", "later", "followUp"), adapter.calls)
        self.assertIn(("interrupt",), adapter.calls)
        self.assertIn(("shutdown_session",), adapter.calls)

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
        for index, action in enumerate(("sendMessage", "followUp", "interrupt", "shutdown")):
            payload = {"message": "x"} if action in {"sendMessage", "followUp"} else {}
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
            agents=(MappingProxyType({"id": "agent", "status": "running"}),),
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
        self.assertEqual(public["subagents"], [{"id": "agent", "status": "running"}])
        self.assertEqual(public["pendingUiRequest"]["id"], "12")
        self.assertTrue(public["pendingUiRequest"]["questions"][0]["multi"])
        self.assertEqual(
            public["pendingUiRequest"]["questions"][0]["checkedIndices"],
            [0],
        )
        self.assertEqual(public["state"], "needs_input")

    def test_close_joins_watcher_and_closes_all_adapters(self):
        manager = self.manager(discovery_interval=0.01)
        session_id, _, extension = self.connect_extension(manager)
        with patch("omp_deck.terminal_sessions.CollabAdapter", FakeCollabAdapter):
            manager.add_collab(collab_link())
        with patch("omp_deck.terminal_sessions.CollabAdapter", FakeCollabAdapter):
            manager.start()
        watcher = manager._watcher
        self.assertIsNotNone(watcher)
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
