from __future__ import annotations

import dataclasses
import enum
import json
import tempfile
import time
import unittest
from pathlib import Path
from threading import Event, Thread, current_thread
from unittest.mock import patch

from omp_rpc import BranchResult, ReadyEvent, RpcProcessExitError, RpcProtocolError, RpcTimeoutError

from omp_deck._rpc_dispatch import SessionCommandConflictError, SessionNotConnectedError
from omp_deck._rpc_errors import AdapterError, AdapterIncompatibleError
from omp_deck.fleet_model import FleetModel
from omp_deck.registry_store import RegistryStore
from omp_deck.rpc_refresh import RpcRefreshScheduler
from omp_deck.rpc_sessions import RpcSessionManager
from omp_deck.settings import Settings


@dataclasses.dataclass(frozen=True)
class _OfficialState:
    session_id: str = "omp-transient"
    session_file: str = ""
    is_streaming: bool = False
    is_compacting: bool = False
    message_count: int = 0
    queued_message_count: int = 0
    thinking_level: str = "medium"
    model: object = None


class _Outcome(enum.Enum):
    COMPLETE = "complete"


@dataclasses.dataclass(frozen=True)
class _Correlation:
    session_id: str
    outcome: _Outcome


@dataclasses.dataclass(frozen=True)
class _Cancellation:
    cancelled: bool


class FakeRpcClient:
    instances: list[FakeRpcClient] = []
    fail_start = False
    fail_stop = False
    fail_new_session = False
    cancel_new_session = False

    cancel_branch = False
    cancel_switch = False
    fail_get_state_once: BaseException | None = None
    prompt_entered: dict[str, Event] = {}
    branch_entered: Event | None = None
    branch_release: Event | None = None
    ui_response_entered: Event | None = None
    prompt_release: dict[str, Event] = {}

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.listeners = {kind: [] for kind in ("ready", "event", "ui", "protocol", "extension", "unknown")}
        self.calls = []
        self.stopped = False
        self.new_session_calls = 0
        self.resumed_path: str | None = None
        self.session_root = Path(kwargs["session_dir"])
        self.instance_number = len(type(self).instances) + 1
        self.state = _OfficialState(
            session_file=str(self.session_root / f"lazy_omp-transient-{self.instance_number}.jsonl")
        )
        if isinstance(kwargs.get("provider"), str) and isinstance(kwargs.get("model"), str):
            self.state = dataclasses.replace(
                self.state,
                model={"provider": kwargs["provider"], "id": kwargs["model"]},
                thinking_level=kwargs.get("thinking"),
            )
        type(self).instances.append(self)
        self.get_state_entered: Event | None = None
        self.get_state_release: Event | None = None
        self.lifecycle_failure: BaseException | None = None
        self.lifecycle_checked = Event()

    def _on(self, kind, listener):
        self.listeners[kind].append(listener)
        return lambda: self.listeners[kind].remove(listener)

    def on_ready(self, listener):
        return self._on("ready", listener)

    def on_event(self, listener):
        return self._on("event", listener)

    def on_ui_request(self, listener):
        return self._on("ui", listener)

    def on_protocol_error(self, listener):
        return self._on("protocol", listener)

    def on_extension_error(self, listener):
        return self._on("extension", listener)

    def on_unknown_notification(self, listener):
        return self._on("unknown", listener)

    def start(self):
        if type(self).fail_start:
            raise RuntimeError("launch failed")
        extra_args = self.kwargs.get("extra_args", ())
        if extra_args:
            session_file = Path(extra_args[1])
            header = self._read_header(session_file)
            self.state = dataclasses.replace(
                self.state,
                session_id=header["id"],
                session_file=str(session_file.resolve()),
            )
            self.resumed_path = str(session_file.resolve())
        ready = ReadyEvent(protocol_version=1, supported_protocol_versions=(1, 2))
        for listener in tuple(self.listeners["ready"]):
            listener(ready)
        return self

    def stop(self):
        self.stopped = True
        if type(self).fail_stop:
            raise RuntimeError("stop failed")

    def collect_events(self, timeout=None):
        self.lifecycle_checked.set()
        if self.lifecycle_failure is not None:
            raise self.lifecycle_failure
        if self.stopped:
            raise RpcProcessExitError("RPC process stopped")
        time.sleep(min(timeout or 0.01, 0.01))
        raise RpcTimeoutError("no terminal event")

    def get_state(self):
        state = self.state
        entered = self.get_state_entered
        release = self.get_state_release
        if entered is not None:
            self.get_state_entered = None
            self.get_state_release = None
            entered.set()
        if release is not None:
            release.wait(timeout=2)
        failure = type(self).fail_get_state_once
        if failure is not None:
            type(self).fail_get_state_once = None
            raise failure
        return state

    def get_messages(self):
        if self.state.message_count == 0:
            return ()
        return ({"id": "message-1", "role": "assistant", "content": "ready"},)

    def get_todos(self):
        return ()

    def request_raw(self, command_type, **payload):
        if command_type == "set_subagent_subscription":
            return {"level": payload["level"]}
        if command_type == "get_subagents":
            return {"subagents": []}
        raise AssertionError(f"unexpected RPC command: {command_type}")

    def _call(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))

    def prompt(self, message, *args, **kwargs):
        self._call("prompt", message, *args, **kwargs)
        entered = type(self).prompt_entered.get(message)
        if entered is not None:
            entered.set()
        release = type(self).prompt_release.get(message)
        if release is not None:
            release.wait(timeout=2)

    def follow_up(self, *args, **kwargs):
        return self._call("follow_up", *args, **kwargs)

    def abort(self):
        return self._call("abort")

    def set_model(self, *args):
        self._call("set_model", *args)
        return _Correlation(self.state.session_id, _Outcome.COMPLETE)

    def set_thinking_level(self, *args):
        return self._call("set_thinking_level", *args)

    def new_session(self):
        self.new_session_calls += 1
        if type(self).fail_new_session:
            raise RuntimeError("new session failed")
        if type(self).cancel_new_session:
            return _Cancellation(True)
        session_id = f"omp-official-{self.instance_number}"
        session_file = self.session_root / f"materialized_{session_id}.jsonl"
        self._write_session(session_file, session_id)
        self.state = dataclasses.replace(self.state, session_id=session_id, session_file=str(session_file.resolve()))
        return _Cancellation(False)

    def branch(self, *args):
        self._call("branch", *args)
        entered = type(self).branch_entered
        if entered is not None:
            entered.set()
        release = type(self).branch_release
        if release is not None:
            release.wait(timeout=2)
        if type(self).cancel_branch:
            return BranchResult(text="", cancelled=True)
        session_file = self.session_root / "omp-branched.jsonl"
        self._write_session(session_file, "omp-branched")
        self.state = dataclasses.replace(
            self.state, session_id="omp-branched", session_file=str(session_file.resolve())
        )
        return BranchResult(text="branched", cancelled=False)

    def switch_session(self, session_path):
        self._call("switch_session", session_path)
        if type(self).cancel_switch:
            return _Cancellation(True)
        resolved = Path(session_path).resolve()
        header = self._read_header(resolved)
        self.state = dataclasses.replace(self.state, session_id=header["id"], session_file=str(resolved))
        return _Correlation(header["id"], _Outcome.COMPLETE)

    def compact(self, *args):
        self._call("compact", *args)
        return _Outcome.COMPLETE

    def send_ui_value(self, *args):
        entered = type(self).ui_response_entered
        if entered is not None:
            entered.set()
        return self._call("send_ui_value", *args)

    def send_ui_confirmation(self, *args):
        return self._call("send_ui_confirmation", *args)

    def cancel_ui_request(self, *args, **kwargs):
        return self._call("cancel_ui_request", *args, **kwargs)

    @staticmethod
    def _write_session(session_file, session_id):
        session_file.parent.mkdir(parents=True, exist_ok=True)
        header = {"type": "session", "version": 3, "id": session_id, "cwd": str(session_file.parent)}
        session_file.write_text(json.dumps(header) + "\n", encoding="utf-8")

    @staticmethod
    def _read_header(session_file):
        with session_file.open(encoding="utf-8") as source:
            first = json.loads(source.readline())
            return json.loads(source.readline()) if first.get("type") == "title" else first

    def emit_event(self, value):
        for listener in tuple(self.listeners["event"]):
            listener(value)

    def emit_ui(self, value):
        for listener in tuple(self.listeners["ui"]):
            listener(value)


class TestRpcSessionManager(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name).resolve()
        self.project = root / "project"
        self.project.mkdir()
        self.settings = Settings(
            host="127.0.0.1",
            port=8787,
            session_root=root / "sessions",
            socket_root=root / "sockets",
            state_root=root / "state",
            project_root=root,
            project_discovery_depth=2,
            omp_bin="/not/a/real/omp",
            allowed_hosts=("127.0.0.1",),
        )
        ids = iter(("deck-stable-id", "deck-second-id"))
        self.registry = RegistryStore(
            self.settings.state_root / "registry.json",
            id_factory=lambda: next(ids),
        )
        self.model = FleetModel()
        FakeRpcClient.instances.clear()
        FakeRpcClient.fail_start = False
        FakeRpcClient.fail_stop = False
        FakeRpcClient.fail_new_session = False
        FakeRpcClient.cancel_new_session = False
        FakeRpcClient.fail_get_state_once = None
        FakeRpcClient.prompt_entered.clear()
        FakeRpcClient.cancel_branch = False
        FakeRpcClient.cancel_switch = False
        FakeRpcClient.prompt_release.clear()
        FakeRpcClient.branch_entered = None
        FakeRpcClient.ui_response_entered = None
        FakeRpcClient.branch_release = None
        self.rpc_patch = patch("omp_deck._rpc_client_factory.RpcClient", FakeRpcClient)
        self.rpc_patch.start()
        self.addCleanup(self.rpc_patch.stop)
        self.manager = RpcSessionManager(self.settings, self.model, self.registry)

        self.addCleanup(lambda: self.manager.close())

    def launch(self):
        return self.manager.launch("project", self.project, title="RPC test")

    def configured_source(self, *, provider="ollama", model="omp-devstral:latest", thinking="high"):
        source = self.launch()
        client = FakeRpcClient.instances[-1]
        client.state = dataclasses.replace(
            client.state,
            model={"provider": provider, "id": model},
            thinking_level=thinking,
        )
        return self.manager.refresh(source["id"])

    def recover(self, manager, model, timeout=2):
        desired = [
            record["id"]
            for record in manager.registry.load()
            if record["adapterKind"] == "rpc" and record["desiredRpcRunning"]
        ]
        cursor = model.journal.latest_id
        updated = set()
        manager.request_refresh_connected()
        deadline = time.monotonic() + timeout
        while desired:
            transports = {session_id: model.get(session_id)["transport"] for session_id in desired}
            if all(
                session_id in updated and transport["status"] not in {"connecting", "reconnecting"}
                for session_id, transport in transports.items()
            ):
                return transports
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.fail(f"recovery did not settle: {transports}")
            events = model.journal.wait_after(cursor, min(remaining, 0.1))
            if events:
                cursor = events[-1].id
                updated.update(event.session_id for event in events)
        return {}

    def test_manager_does_not_project_terminal_tombstones(self):
        self.registry.create(
            session_id="terminal-hidden",
            adapter_kind="extension",
            omp_session_id="omp-terminal",
            project_name="terminal",
            project_path=str(self.project),
            title="Removed terminal",
            recovery_metadata={"connectionId": "connection-1", "removed": True},
        )
        self.manager.close()
        model = FleetModel()
        self.manager = RpcSessionManager(self.settings, model, self.registry)

        with self.assertRaises(KeyError):
            model.get("terminal-hidden")

    def test_launch_persists_stable_deck_id_and_official_rpc_identity(self):
        session = self.launch()
        record = self.registry.load()[0]

        self.assertEqual(session["id"], "deck-stable-id")
        self.assertEqual(session["ompSessionId"], "omp-official-1")
        self.assertEqual(record["id"], "deck-stable-id")
        self.assertEqual(record["ompSessionId"], "omp-official-1")
        expected_file = self.settings.session_root / "materialized_omp-official-1.jsonl"
        self.assertEqual(record["recoveryMetadata"], {"sessionFile": str(expected_file)})
        self.assertTrue(expected_file.is_file())
        self.assertEqual(FakeRpcClient.instances[-1].new_session_calls, 1)
        self.assertTrue(record["desiredRpcRunning"])
        self.assertEqual(session["transport"]["status"], "connected")

    def test_start_fresh_preserves_settings_and_source_and_replays_duplicate(self):
        source = self.configured_source()
        source_client = FakeRpcClient.instances[-1]
        source_client.state = dataclasses.replace(source_client.state, message_count=1)
        source_before = self.manager.refresh(source["id"])

        first = self.manager.fresh_request(source["id"], "fresh-request")
        duplicate = self.manager.fresh_request(source["id"], "fresh-request")

        fresh = first["session"]
        self.assertEqual(first["status"], "accepted")
        self.assertEqual(duplicate["session"]["id"], fresh["id"])
        self.assertNotEqual(fresh["id"], source["id"])
        self.assertNotEqual(fresh["ompSessionId"], source["ompSessionId"])
        self.assertEqual(fresh["project"], source["project"])
        self.assertEqual(fresh["model"], source["model"])
        self.assertEqual(fresh["thinking"], source["thinking"])
        self.assertEqual(fresh["messages"], [])
        self.assertEqual(self.model.get(source["id"]), source_before)
        self.assertEqual(len(FakeRpcClient.instances), 2)

    def test_concurrent_fresh_retry_launches_once(self):
        source = self.configured_source()
        entered, release = Event(), Event()
        original_start = FakeRpcClient.start
        outcomes, errors = [], []

        def delayed_start(client):
            if client.instance_number == 2:
                entered.set()
                if not release.wait(3):
                    raise TimeoutError("fresh launch was not released")
            return original_start(client)

        def fresh():
            try:
                outcomes.append(self.manager.fresh_request(source["id"], "concurrent-fresh"))
            except Exception as error:
                errors.append(error)

        first = Thread(target=fresh)
        second = Thread(target=fresh)
        with patch.object(FakeRpcClient, "start", delayed_start):
            first.start()
            self.assertTrue(entered.wait(2))
            second.start()
            source_client = FakeRpcClient.instances[0]
            source_client.state = dataclasses.replace(
                source_client.state,
                model={"provider": "changed", "id": "later-model"},
                thinking_level="low",
            )
            self.manager.refresh(source["id"])
            release.set()
            first.join(4)
            second.join(4)

        self.assertEqual(errors, [])
        self.assertEqual(len(outcomes), 2)
        self.assertEqual(outcomes[0]["session"]["id"], outcomes[1]["session"]["id"])
        self.assertEqual(len(FakeRpcClient.instances), 2)
        self.assertEqual(
            outcomes[0]["session"]["model"],
            {"provider": "ollama", "id": "omp-devstral:latest"},
        )
        self.assertEqual(outcomes[0]["session"]["thinking"], "high")
        self.assertEqual(
            self.model.get(source["id"])["model"],
            {"provider": "changed", "id": "later-model"},
        )

    def test_empty_fresh_session_recovers_native_model_and_thinking(self):
        persisted = {}
        start = FakeRpcClient.start

        def set_model(client, provider, model):
            client.state = dataclasses.replace(client.state, model={"provider": provider, "id": model})
            persisted[client.state.session_file] = client.state

        def set_thinking(client, level):
            client.state = dataclasses.replace(client.state, thinking_level=level)
            persisted[client.state.session_file] = client.state

        def resume(client):
            extra_args = client.kwargs.get("extra_args", ())
            if extra_args:
                saved = persisted.get(str(Path(extra_args[1]).resolve()))
                client.state = dataclasses.replace(
                    client.state,
                    model=saved.model if saved else {"provider": "default", "id": "fallback"},
                    thinking_level=saved.thinking_level if saved else "medium",
                )
            return start(client)

        with (
            patch.object(FakeRpcClient, "set_model", set_model),
            patch.object(FakeRpcClient, "set_thinking_level", set_thinking),
            patch.object(FakeRpcClient, "start", resume),
        ):
            source = self.configured_source(thinking="low")
            fresh = self.manager.fresh_request(source["id"], "durable-fresh")["session"]
            self.manager.close()
            recovered_model = FleetModel()
            self.manager = RpcSessionManager(self.settings, recovered_model, self.registry)
            self.recover(self.manager, recovered_model)
            recovered = self.manager.refresh(fresh["id"])

        self.assertEqual(recovered["transport"]["status"], "connected", recovered["transport"])
        self.assertEqual(recovered["ompSessionId"], fresh["ompSessionId"])
        self.assertEqual(recovered["model"], source["model"])
        self.assertEqual(recovered["thinking"], "low")
        self.assertEqual(recovered["messages"], [])

    def test_fresh_replay_survives_manager_restart_and_source_removal(self):
        source = self.configured_source()
        accepted = self.manager.fresh_request(source["id"], "restart-fresh")
        self.manager.close()
        self.registry.remove(source["id"])
        restarted_model = FleetModel()
        self.manager = RpcSessionManager(self.settings, restarted_model, self.registry)

        replay = self.manager.fresh_request(source["id"], "restart-fresh")

        self.assertEqual(replay["status"], "accepted")
        self.assertEqual(replay["session"]["id"], accepted["session"]["id"])
        self.assertEqual(len(FakeRpcClient.instances), 2)

    def test_fresh_rejects_unknown_settings_without_starting_native_process(self):
        source = self.launch()

        first = self.manager.fresh_request(source["id"], "missing-settings")
        duplicate = self.manager.fresh_request(source["id"], "missing-settings")

        expected = {
            "requestId": "missing-settings",
            "status": "failed",
            "errorCode": "source_settings_unavailable",
        }
        self.assertEqual(first, expected)
        self.assertEqual(duplicate, expected)
        self.assertEqual(len(FakeRpcClient.instances), 1)

    def test_fresh_rejects_project_outside_configured_root(self):
        source = self.configured_source()
        outside = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        with self.model._lock:
            self.model._sessions[source["id"]]["project"] = {
                "name": "outside",
                "path": str(outside),
            }

        result = self.manager.fresh_request(source["id"], "escaped-project")

        self.assertEqual(
            result,
            {
                "requestId": "escaped-project",
                "status": "failed",
                "errorCode": "invalid_project",
            },
        )
        self.assertEqual(len(FakeRpcClient.instances), 1)

    def test_fresh_closes_and_removes_native_session_with_mismatched_settings(self):
        source = self.configured_source()
        original_state = FakeRpcClient.get_state

        def mismatched_state(client):
            state = original_state(client)
            if client.instance_number == 2:
                return dataclasses.replace(
                    state,
                    model={"provider": "fallback", "id": "other-model"},
                )
            return state

        with patch.object(FakeRpcClient, "get_state", mismatched_state):
            result = self.manager.fresh_request(source["id"], "mismatched-fresh")

        self.assertEqual(
            result,
            {
                "requestId": "mismatched-fresh",
                "status": "failed",
                "errorCode": "settings_mismatch",
            },
        )
        self.assertTrue(FakeRpcClient.instances[-1].stopped)
        self.assertEqual([record["id"] for record in self.registry.load()], [source["id"]])
        with self.assertRaises(KeyError):
            self.model.get("deck-second-id")

    def test_fresh_native_startup_failure_is_closed_and_durably_replayed(self):
        source = self.configured_source()
        FakeRpcClient.fail_start = True

        first = self.manager.fresh_request(source["id"], "failed-fresh-start")
        duplicate = self.manager.fresh_request(source["id"], "failed-fresh-start")

        expected = {
            "requestId": "failed-fresh-start",
            "status": "failed",
            "errorCode": "launch_failed",
        }
        self.assertEqual(first, expected)
        self.assertEqual(duplicate, expected)
        self.assertTrue(FakeRpcClient.instances[-1].stopped)
        self.assertEqual(len(FakeRpcClient.instances), 2)
        self.assertIsNone(self.manager._runtime_or_none("deck-second-id"))

    def test_refresh_during_startup_does_not_stop_or_replace_the_launching_process(self):
        start_entered, release_start, started, refresh_entered = (Event() for _ in range(4))
        outcomes, errors = [], []
        original_start = FakeRpcClient.start
        original_state = FakeRpcClient.get_state
        operation_lock = self.manager._operation_lock

        def delayed_start(client):
            start_entered.set()
            if not release_start.wait(3):
                raise TimeoutError("startup was not released")
            result = original_start(client)
            started.set()
            return result

        def state_when_ready(client):
            if not started.is_set() or client.stopped:
                raise RpcProcessExitError("RPC process is not ready")
            return original_state(client)

        def launch():
            try:
                outcomes.append(self.manager.launch_request("concurrent-launch", "project"))
            except Exception as error:
                errors.append(error)

        def observed_lock(session_id):
            if current_thread() is refresh_thread:
                refresh_entered.set()
            return operation_lock(session_id)

        launch_thread = Thread(target=launch)
        refresh_thread = Thread(target=self.manager.refresh_connected)
        with (
            patch.object(FakeRpcClient, "start", delayed_start),
            patch.object(FakeRpcClient, "get_state", state_when_ready),
            patch.object(self.manager, "_operation_lock", side_effect=observed_lock),
        ):
            launch_thread.start()
            try:
                self.assertTrue(start_entered.wait(2))
                client = FakeRpcClient.instances[0]
                refresh_thread.start()
                self.assertTrue(refresh_entered.wait(2))
            finally:
                release_start.set()
                launch_thread.join(4)
                if refresh_thread.ident is not None:
                    refresh_thread.join(4)

        self.assertFalse(launch_thread.is_alive())
        self.assertFalse(refresh_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(outcomes[0]["status"], "accepted")
        self.assertFalse(client.stopped)
        self.assertEqual(outcomes[0]["session"]["ompSessionId"], client.state.session_id)
        self.assertEqual(self.manager.refresh("deck-stable-id")["transport"]["status"], "connected")

    def test_cancelled_fresh_materialization_fails_launch_and_cleans_up(self):
        FakeRpcClient.cancel_new_session = True

        with self.assertRaisesRegex(RuntimeError, "cancelled fresh RPC session materialization"):
            self.launch()

        client = FakeRpcClient.instances[-1]
        self.assertTrue(client.stopped)
        self.assertFalse(Path(client.state.session_file).exists())
        self.assertFalse(self.registry.load()[0]["desiredRpcRunning"])
        self.assertIsNone(self.manager._runtime_or_none("deck-stable-id"))

    def test_failed_fresh_materialization_fails_launch_and_cleans_up(self):
        FakeRpcClient.fail_new_session = True

        with self.assertRaisesRegex(AdapterError, "new session failed"):
            self.launch()

        self.assertTrue(FakeRpcClient.instances[-1].stopped)
        self.assertFalse(self.registry.load()[0]["desiredRpcRunning"])
        self.assertIsNone(self.manager._runtime_or_none("deck-stable-id"))

    def test_completed_launch_request_is_not_replayed_after_manager_restart(self):
        first = self.manager.launch_request("durable-launch", "project", title="RPC test")
        self.manager.close()
        self.manager = RpcSessionManager(self.settings, FleetModel(), self.registry)

        duplicate = self.manager.launch_request("durable-launch", "project", title="RPC test")

        self.assertEqual(duplicate["status"], "accepted")
        self.assertEqual(duplicate["session"]["id"], first["session"]["id"])
        self.assertEqual(len(FakeRpcClient.instances), 1)

    def test_failed_created_launch_preserves_directory_and_durable_outcome(self):
        def fail_after_writing(_project_name, project_path, *, title=None):
            (project_path / "work-in-progress.txt").write_text("preserve me")
            raise RuntimeError("launch failed")

        with patch.object(self.manager, "launch", side_effect=fail_after_writing) as launch:
            result = self.manager.launch_request(
                "failed-created-launch",
                "new-project",
                create=True,
            )
            duplicate = self.manager.launch_request(
                "failed-created-launch",
                "new-project",
                create=True,
            )

        expected = {
            "requestId": "failed-created-launch",
            "status": "failed",
            "errorCode": "launch_failed",
            "created": True,
        }
        self.assertEqual(result, expected)
        self.assertEqual(duplicate, expected)
        self.assertEqual(
            (self.settings.project_root / "new-project/work-in-progress.txt").read_text(),
            "preserve me",
        )
        self.assertEqual(launch.call_count, 1)

    def test_callback_updates_normalized_public_state(self):
        session_id = self.launch()["id"]
        FakeRpcClient.instances[-1].emit_event({"type": "turn_start", "turn": 4})

        session = self.model.get(session_id)
        self.assertEqual(session["state"], "working")
        self.assertEqual(self.model.journal.replay_after(0)[-1].sequence, 2)

    def test_commands_return_json_compatible_native_results(self):
        session_id = self.launch()["id"]
        switch_target = self.settings.session_root / "other.jsonl"
        FakeRpcClient._write_session(switch_target, "omp-switched")
        commands = (
            ("sendMessage", {"message": "hello", "images": [{"data": "x"}], "streamingBehavior": "steer"}),
            ("followUp", {"message": "later", "images": []}),
            ("interrupt", {}),
            ("setModel", {"provider": "openai", "modelId": "gpt"}),
            ("setThinking", {"level": "high"}),
            ("branch", {"entryId": "entry-7"}),
            ("switchSession", {"sessionPath": str(switch_target)}),
            ("compact", {"customInstructions": "short"}),
        )
        results = {action: self.manager.command(session_id, action, payload) for action, payload in commands}

        self.assertEqual(results["setModel"]["result"], {"session_id": "omp-official-1", "outcome": "complete"})
        self.assertEqual(results["branch"]["result"], {"text": "branched", "cancelled": False})
        self.assertEqual(results["switchSession"]["result"]["session_id"], "omp-switched")
        self.assertEqual(results["compact"]["result"], "complete")
        self.assertTrue(all(result["accepted"] for result in results.values()))

    def test_native_branch_and_switch_cancellation_are_durable_cancellations(self):
        session_id = self.launch()["id"]
        original_identity = self.registry.load()[0]["ompSessionId"]
        switch_target = self.settings.session_root / "other.jsonl"
        FakeRpcClient._write_session(switch_target, "omp-switched")
        FakeRpcClient.cancel_branch = True

        branch = self.manager.dispatch(session_id, "cancel-branch", "branch", {"entryId": "entry-7"})
        branch_replay = self.manager.dispatch(session_id, "cancel-branch", "branch", {"entryId": "entry-7"})
        FakeRpcClient.cancel_switch = True
        switched = self.manager.dispatch(
            session_id,
            "cancel-switch",
            "switchSession",
            {"sessionPath": str(switch_target)},
        )

        expected_branch = {
            "requestId": "cancel-branch",
            "status": "cancelled",
            "errorCode": "cancelled",
        }
        self.assertEqual(branch, expected_branch)
        self.assertEqual(branch_replay, expected_branch)
        self.assertEqual(
            switched,
            {
                "requestId": "cancel-switch",
                "status": "cancelled",
                "errorCode": "cancelled",
            },
        )
        self.assertEqual(self.registry.load()[0]["ompSessionId"], original_identity)
        calls = [call[0] for call in FakeRpcClient.instances[-1].calls]
        self.assertEqual(calls.count("branch"), 1)
        self.assertEqual(calls.count("switch_session"), 1)

    def test_shutdown_clears_desired_state_and_disconnects(self):
        session_id = self.launch()["id"]
        client = FakeRpcClient.instances[-1]
        result = self.manager.command(session_id, "shutdown", {})

        self.assertEqual(result, {"accepted": True, "result": None})
        self.assertTrue(client.stopped)
        self.assertFalse(self.registry.load()[0]["desiredRpcRunning"])
        self.assertEqual(self.model.get(session_id)["transport"]["status"], "disconnected")
        self.assertEqual(self.model.get(session_id)["transport"]["reason"], "shut down")

    def test_shutdown_stops_a_blocked_rpc_command_without_waiting_for_its_operation_lock(self):
        session_id = self.launch()["id"]
        client = FakeRpcClient.instances[-1]
        prompt_entered = Event()
        prompt_release = Event()
        FakeRpcClient.prompt_entered["blocked"] = prompt_entered
        FakeRpcClient.prompt_release["blocked"] = prompt_release
        command_results = []
        shutdown_results = []
        errors = []

        def dispatch_command():
            try:
                command_results.append(
                    self.manager.dispatch(session_id, "blocked-command", "sendMessage", {"message": "blocked"})
                )
            except Exception as error:
                errors.append(error)

        def dispatch_shutdown():
            try:
                shutdown_results.append(self.manager.dispatch(session_id, "quit-now", "shutdown", {}))
            except Exception as error:
                errors.append(error)

        original_stop = client.stop

        def releasing_stop():
            prompt_release.set()
            original_stop()

        command = Thread(target=dispatch_command)
        shutdown = Thread(target=dispatch_shutdown)
        with patch.object(client, "stop", side_effect=releasing_stop):
            command.start()
            self.assertTrue(prompt_entered.wait(timeout=1))
            shutdown.start()
            shutdown.join(timeout=0.5)
            command.join(timeout=1)
        prompt_release.set()
        shutdown.join(timeout=1)
        command.join(timeout=1)

        self.assertFalse(shutdown.is_alive())
        self.assertFalse(command.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(
            shutdown_results,
            [{"requestId": "quit-now", "status": "accepted", "result": None}],
        )
        self.assertEqual(command_results[0]["status"], "accepted")
        self.assertTrue(client.stopped)
        self.assertEqual(self.model.get(session_id)["lifecycle"], "history")

    def test_shutdown_preserves_a_completed_identity_change_without_reclaiming_live_ownership(self):
        session_id = self.launch()["id"]
        runtime = self.manager._runtime(session_id)
        adapter = runtime.adapter
        snapshot_captured = Event()
        release_snapshot = Event()
        original_snapshot = adapter.snapshot
        branch_results = []
        errors = []

        def delayed_snapshot():
            snapshot = original_snapshot()
            snapshot_captured.set()
            release_snapshot.wait(2)
            return snapshot

        def dispatch_branch():
            try:
                branch_results.append(
                    self.manager.dispatch(session_id, "branch-before-quit", "branch", {"entryId": "entry-7"})
                )
            except Exception as error:
                errors.append(error)

        branch = Thread(target=dispatch_branch)
        with patch.object(adapter, "snapshot", side_effect=delayed_snapshot):
            branch.start()
            try:
                self.assertTrue(snapshot_captured.wait(1))
                shutdown = self.manager.dispatch(session_id, "quit-after-branch", "shutdown", {})
            finally:
                release_snapshot.set()
                branch.join(1)

        record = self.registry.load()[0]
        self.assertFalse(branch.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(shutdown["status"], "accepted")
        self.assertEqual(branch_results[0]["status"], "accepted")
        self.assertEqual(record["ompSessionId"], "omp-branched")
        self.assertFalse(record["desiredRpcRunning"])
        session = self.model.get(session_id)
        self.assertEqual(session["ompSessionId"], "omp-branched")
        self.assertEqual(session["lifecycle"], "history")
        self.assertEqual(session["transport"]["reason"], "shut down")

    def test_disconnected_failed_recovery_can_be_shut_down_durably(self):
        session_id = self.launch()["id"]
        self.manager.close()
        FakeRpcClient.fail_start = True
        recovering_model = FleetModel()
        recovering = RpcSessionManager(self.settings, recovering_model, self.registry)
        self.manager = recovering
        transports = self.recover(recovering, recovering_model)
        self.assertEqual(transports[session_id]["reason"], "recovery failed: launch failed")

        result = recovering.command(session_id, "shutdown", {})
        self.assertEqual(result, {"accepted": True, "result": None})
        self.assertFalse(self.registry.load()[0]["desiredRpcRunning"])

        recovering.close()
        restarted_model = FleetModel()
        restarted = RpcSessionManager(self.settings, restarted_model, self.registry)
        self.manager = restarted
        self.assertEqual(self.recover(restarted, restarted_model), {})
        self.assertEqual(len(FakeRpcClient.instances), 2)

    def test_shutdown_failure_retains_runtime_ownership_for_retry(self):
        session_id = self.launch()["id"]
        FakeRpcClient.fail_stop = True

        with self.assertRaisesRegex(RuntimeError, "stop failed"):
            self.manager.shutdown(session_id)

        self.assertFalse(self.registry.load()[0]["desiredRpcRunning"])
        self.assertIsNotNone(self.manager._runtime_or_none(session_id))
        transport = self.model.get(session_id)["transport"]
        self.assertEqual(transport["status"], "disconnected")
        self.assertEqual(transport["reason"], "shutdown failed")

        FakeRpcClient.fail_stop = False
        self.manager.shutdown(session_id)
        self.assertIsNone(self.manager._runtime_or_none(session_id))
        self.assertTrue(FakeRpcClient.instances[-1].stopped)

    def test_shutdown_registry_failure_keeps_live_runtime_owned(self):
        session_id = self.launch()["id"]
        runtime = self.manager._runtime(session_id)
        client = FakeRpcClient.instances[-1]

        with patch.object(self.registry, "update", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.manager.shutdown(session_id)

        self.assertIs(self.manager._runtime_or_none(session_id), runtime)
        self.assertFalse(client.stopped)
        self.assertTrue(self.registry.load()[0]["desiredRpcRunning"])

    def test_close_stops_transport_but_preserves_restart_intent(self):
        session_id = self.launch()["id"]
        client = FakeRpcClient.instances[-1]
        self.manager.close()

        self.assertTrue(client.stopped)
        self.assertTrue(self.registry.load()[0]["desiredRpcRunning"])
        self.assertEqual(self.model.get(session_id)["transport"]["status"], "disconnected")
        self.assertEqual(self.model.get(session_id)["transport"]["reason"], "closed")

    def test_close_reports_failed_transport_and_retains_runtime(self):
        session_id = self.launch()["id"]
        FakeRpcClient.fail_stop = True
        try:
            with self.assertRaisesRegex(
                RuntimeError,
                "RPC session shutdown failed.*stop failed",
            ):
                self.manager.close(timeout=0.2)
            self.assertIsNotNone(self.manager._runtime_or_none(session_id))
        finally:
            FakeRpcClient.fail_stop = False
        self.manager.close()
        self.assertIsNone(self.manager._runtime_or_none(session_id))

    def test_close_is_bounded_when_session_operation_does_not_finish(self):
        session_id = self.launch()["id"]
        entered = Event()
        release = Event()

        def hold_operation():
            with self.manager._operation_lock(session_id):
                entered.set()
                release.wait()

        worker = Thread(target=hold_operation, daemon=True)
        worker.start()
        self.assertTrue(entered.wait(timeout=1))
        started = time.monotonic()
        try:
            with self.assertRaisesRegex(
                RuntimeError,
                "session did not become idle",
            ):
                self.manager.close(timeout=0.01)
            self.assertLess(time.monotonic() - started, 0.2)
            self.assertIsNotNone(self.manager._runtime_or_none(session_id))
        finally:
            release.set()
            worker.join(timeout=1)
        self.manager.close()

    def test_empty_launch_is_materialized_and_recovers_the_same_omp_session(self):
        original = self.launch()
        record = self.registry.load()[0]
        session_file = Path(record["recoveryMetadata"]["sessionFile"])
        self.manager.command(original["id"], "setThinking", {"level": "high"})
        self.assertEqual(FakeRpcClient.instances[-1].get_messages(), ())
        self.assertTrue(session_file.is_file())
        self.assertEqual(FakeRpcClient._read_header(session_file)["id"], record["ompSessionId"])
        self.manager.close()
        recovering_model = FleetModel()
        recovering = RpcSessionManager(self.settings, recovering_model, self.registry)
        self.addCleanup(recovering.close)

        self.assertEqual(self.recover(recovering, recovering_model)[original["id"]]["status"], "connected")
        client = FakeRpcClient.instances[-1]
        self.assertEqual(client.resumed_path, str(session_file))
        self.assertEqual(client.new_session_calls, 0)
        self.assertEqual(client.state.session_id, record["ompSessionId"])
        self.assertEqual(client.kwargs["cwd"], str(self.project))
        self.assertEqual(recovering_model.get(original["id"])["id"], "deck-stable-id")
        self.assertEqual(recovering_model.get(original["id"])["ompSessionId"], record["ompSessionId"])

    def test_recovery_refuses_a_missing_session_file_without_spawning_a_replacement(self):
        session_id = self.launch()["id"]
        record = self.registry.load()[0]
        self.manager.close()
        Path(record["recoveryMetadata"]["sessionFile"]).unlink()
        recovering_model = FleetModel()
        recovering = RpcSessionManager(self.settings, recovering_model, self.registry)
        self.addCleanup(recovering.close)

        transports = self.recover(recovering, recovering_model)

        self.assertIn("session file is missing or inaccessible", transports[session_id]["reason"])
        self.assertEqual(len(FakeRpcClient.instances), 1)
        self.assertEqual(self.registry.load()[0]["ompSessionId"], record["ompSessionId"])
        self.assertEqual(recovering_model.get(session_id)["transport"]["status"], "disconnected")

    def test_recovery_refuses_a_session_file_outside_the_configured_root(self):
        session_id = self.launch()["id"]
        record = self.registry.load()[0]
        outside = self.settings.project_root / "outside.jsonl"
        FakeRpcClient._write_session(outside, record["ompSessionId"])
        self.manager.close()
        self.registry.update(session_id, recovery_metadata={"sessionFile": str(outside)})
        recovering_model = FleetModel()
        recovering = RpcSessionManager(self.settings, recovering_model, self.registry)
        self.addCleanup(recovering.close)

        transports = self.recover(recovering, recovering_model)

        self.assertIn("outside the configured session root", transports[session_id]["reason"])
        self.assertEqual(len(FakeRpcClient.instances), 1)

    def test_recovery_refuses_a_symlinked_session_file(self):
        session_id = self.launch()["id"]
        record = self.registry.load()[0]
        original = Path(record["recoveryMetadata"]["sessionFile"])
        alias = original.with_name("alias.jsonl")
        alias.symlink_to(original)
        self.manager.close()
        self.registry.update(session_id, recovery_metadata={"sessionFile": str(alias)})
        recovering_model = FleetModel()
        recovering = RpcSessionManager(self.settings, recovering_model, self.registry)
        self.addCleanup(recovering.close)

        transports = self.recover(recovering, recovering_model)

        self.assertIn("must not use filesystem aliases", transports[session_id]["reason"])
        self.assertEqual(len(FakeRpcClient.instances), 1)

    def test_recovery_refuses_a_malformed_session_without_replacing_it(self):
        session_id = self.launch()["id"]
        record = self.registry.load()[0]
        session_file = Path(record["recoveryMetadata"]["sessionFile"])
        self.manager.close()
        session_file.write_text("{not-json}\n", encoding="utf-8")
        recovering_model = FleetModel()
        recovering = RpcSessionManager(self.settings, recovering_model, self.registry)
        self.addCleanup(recovering.close)

        transports = self.recover(recovering, recovering_model)

        self.assertIn("missing or malformed header", transports[session_id]["reason"])
        self.assertEqual(len(FakeRpcClient.instances), 1)
        self.assertEqual(self.registry.load()[0]["ompSessionId"], record["ompSessionId"])

    def test_branch_identity_is_persisted_and_recovered(self):
        session_id = self.launch()["id"]
        self.manager.command(session_id, "branch", {"entryId": "entry-7"})
        record = self.registry.load()[0]
        self.assertEqual(record["ompSessionId"], "omp-branched")
        self.assertEqual(
            record["recoveryMetadata"],
            {"sessionFile": str(self.settings.session_root / "omp-branched.jsonl")},
        )

        self.manager.close()
        recovering_model = FleetModel()
        recovering = RpcSessionManager(self.settings, recovering_model, self.registry)
        self.addCleanup(recovering.close)
        self.assertEqual(self.recover(recovering, recovering_model)[session_id]["status"], "connected")
        self.assertEqual(
            FakeRpcClient.instances[-1].resumed_path,
            record["recoveryMetadata"]["sessionFile"],
        )

    def test_switch_identity_is_persisted_and_recovered(self):
        session_id = self.launch()["id"]
        switch_target = self.settings.session_root / "other.jsonl"
        FakeRpcClient._write_session(switch_target, "omp-switched")
        self.manager.command(session_id, "switchSession", {"sessionPath": str(switch_target)})
        record = self.registry.load()[0]
        self.assertEqual(record["ompSessionId"], "omp-switched")
        self.assertEqual(record["recoveryMetadata"], {"sessionFile": str(switch_target)})

        self.manager.close()
        recovering_model = FleetModel()
        recovering = RpcSessionManager(self.settings, recovering_model, self.registry)
        self.addCleanup(recovering.close)
        self.assertEqual(self.recover(recovering, recovering_model)[session_id]["status"], "connected")
        self.assertEqual(FakeRpcClient.instances[-1].resumed_path, str(switch_target))

    def test_refresh_captured_before_branch_cannot_restore_old_identity(self):
        session_id = self.launch()["id"]
        client = FakeRpcClient.instances[-1]
        refresh_entered = Event()
        refresh_release = Event()
        client.get_state_entered = refresh_entered
        client.get_state_release = refresh_release
        refresh = Thread(target=self.manager.refresh, args=(session_id,), daemon=True)
        refresh.start()
        try:
            self.assertTrue(refresh_entered.wait(timeout=1))
            self.manager.command(session_id, "branch", {"entryId": "entry-7"})
        finally:
            refresh_release.set()
            refresh.join(timeout=1)

        self.assertFalse(refresh.is_alive())
        record = self.registry.load()[0]
        self.assertEqual(record["ompSessionId"], "omp-branched")
        self.assertEqual(
            record["recoveryMetadata"], {"sessionFile": str(self.settings.session_root / "omp-branched.jsonl")}
        )

    def test_newer_same_owner_event_wins_over_a_captured_snapshot_without_degrading(self):
        session_id = self.launch()["id"]
        client = FakeRpcClient.instances[-1]
        adapter = self.manager._runtime(session_id).adapter
        original_snapshot = adapter.snapshot

        def snapshot_then_event():
            snapshot = original_snapshot()
            client.emit_event({"type": "state_changed"})
            return snapshot

        with patch.object(adapter, "snapshot", side_effect=snapshot_then_event):
            session = self.manager.refresh(session_id)

        self.assertEqual(session["transport"]["status"], "connected")
        self.assertIsNone(session["transport"]["reason"])
        self.assertEqual(self.manager._runtime(session_id).adapter.transport_status, "connected")

    def test_repeated_recovery_does_not_mutate_generation_or_spawn_again(self):
        session_id = self.launch()["id"]
        self.manager.close()
        recovering_model = FleetModel()
        recovering = RpcSessionManager(self.settings, recovering_model, self.registry)
        self.addCleanup(recovering.close)

        self.assertEqual(self.recover(recovering, recovering_model)[session_id]["status"], "connected")
        generation = self.registry.load()[0]["generation"]
        child_count = len(FakeRpcClient.instances)
        event_id = recovering_model.journal.latest_id
        recovering.request_refresh_connected()
        recovering_model.journal.wait_after(event_id, timeout=1)

        self.assertEqual(self.registry.load()[0]["generation"], generation)
        self.assertEqual(len(FakeRpcClient.instances), child_count)
        self.assertIsNotNone(recovering_model.get(session_id)["transport"]["instanceId"])

    def test_recovery_start_failure_is_explicitly_disconnected(self):
        session_id = self.launch()["id"]
        self.manager.close()
        FakeRpcClient.fail_start = True
        recovering_model = FleetModel()
        recovering = RpcSessionManager(self.settings, recovering_model, self.registry)
        self.addCleanup(recovering.close)

        transport = self.recover(recovering, recovering_model)[session_id]
        self.assertEqual(transport["status"], "disconnected")
        self.assertEqual(transport["reason"], "recovery failed: launch failed")
        self.assertTrue(FakeRpcClient.instances[-1].stopped)

    def test_refresh_process_exit_reaps_and_replaces_child_under_same_deck_id(self):
        session_id = self.launch()["id"]
        original = FakeRpcClient.instances[-1]
        FakeRpcClient.fail_get_state_once = RpcProcessExitError("RPC process exited")

        self.manager.refresh_connected()

        self.assertTrue(original.stopped)
        self.assertEqual(len(FakeRpcClient.instances), 2)
        replacement = FakeRpcClient.instances[-1]
        self.assertEqual(replacement.resumed_path, self.registry.load()[0]["recoveryMetadata"]["sessionFile"])
        self.assertEqual(self.model.get(session_id)["id"], session_id)
        self.assertEqual(
            self.model.get(session_id)["transport"]["status"],
            "connected",
            self.model.snapshot(),
        )

    def test_idle_lifecycle_failure_recovers_without_refresh_or_browser_traffic(self):
        session_id = self.launch()["id"]
        original_runtime = self.manager._runtime(session_id)
        original = FakeRpcClient.instances[-1]
        late_protocol_error = original.listeners["protocol"][0]
        self.assertTrue(original.lifecycle_checked.wait(timeout=1))

        original.lifecycle_failure = RpcProcessExitError("RPC reader terminated")
        # The replacement client exists before its connection is published.
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and (
            len(FakeRpcClient.instances) < 2 or self.model.get(session_id)["transport"]["status"] != "connected"
        ):
            time.sleep(0.01)

        replacement = self.manager._runtime(session_id)
        self.assertIsNot(replacement, original_runtime)
        self.assertTrue(original.stopped)
        self.assertEqual(self.model.get(session_id)["transport"]["status"], "connected")

        instance_count = len(FakeRpcClient.instances)
        self.manager._replace_disconnected(
            session_id,
            RpcProcessExitError("duplicate stale watcher result"),
            expected_runtime=original_runtime,
        )
        self.assertIs(self.manager._runtime(session_id), replacement)
        self.assertEqual(len(FakeRpcClient.instances), instance_count)

        journal_id = self.model.journal.latest_id
        stale_error = RpcProtocolError(
            {
                "type": "response",
                "id": "req_old",
                "command": "prompt",
                "success": False,
                "error": "late old-generation failure",
            }
        )
        late_protocol_error(stale_error)
        late_protocol_error(stale_error)
        self.assertEqual(self.model.journal.latest_id, journal_id)

    def test_intentional_shutdown_stops_supervision_without_recovery(self):
        session_id = self.launch()["id"]
        client = FakeRpcClient.instances[-1]
        self.assertTrue(client.lifecycle_checked.wait(timeout=1))

        self.manager.shutdown(session_id)
        client.lifecycle_failure = RpcProcessExitError("closed intentionally")
        time.sleep(0.15)

        self.assertEqual(len(FakeRpcClient.instances), 1)
        self.assertIsNone(self.manager._runtime_or_none(session_id))
        self.assertFalse(self.registry.load()[0]["desiredRpcRunning"])

    def test_snapshot_contention_retries_without_degrading_healthy_transport(self):
        session_id = self.launch()["id"]
        adapter = self.manager._runtime(session_id).adapter
        calls = []

        def busy_snapshot():
            calls.append(None)
            raise AdapterError("state kept changing", kind="snapshot_busy")

        with patch.object(adapter, "snapshot", side_effect=busy_snapshot):
            self.manager._refreshes.refresh_now((session_id,))

        self.assertEqual(len(calls), 2)
        transport = self.model.get(session_id)["transport"]
        self.assertEqual(transport["status"], "connected")
        self.assertIsNone(transport["reason"])

    def test_background_recovery_does_not_let_a_slow_session_block_another(self):
        slow_id = self.launch()["id"]
        second_project = self.settings.project_root / "second"
        second_project.mkdir()
        fast_id = self.manager.launch("second", second_project)["id"]
        self.manager.close()
        recovering_model = FleetModel()
        recovering = RpcSessionManager(self.settings, recovering_model, self.registry)
        self.manager = recovering
        slow_entered = Event()
        release_slow = Event()
        original_start = FakeRpcClient.start

        def selective_start(client):
            if client.kwargs["cwd"] == str(self.project):
                slow_entered.set()
                release_slow.wait(2)
            return original_start(client)

        with patch.object(FakeRpcClient, "start", selective_start):
            recovering.request_refresh_connected()
            self.assertTrue(slow_entered.wait(1))
            deadline = time.monotonic() + 1
            while recovering_model.get(fast_id)["transport"]["status"] != "connected" and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(recovering_model.get(fast_id)["transport"]["status"], "connected")
            self.assertEqual(recovering_model.get(slow_id)["transport"]["status"], "reconnecting")
            release_slow.set()

    def test_background_refresh_isolates_a_blocked_session(self):
        first_id = self.launch()["id"]
        first_client = FakeRpcClient.instances[-1]
        second_project = self.settings.project_root / "second"
        second_project.mkdir()
        second_id = self.manager.launch("second", second_project)["id"]
        second_client = FakeRpcClient.instances[-1]
        first_entered = Event()
        first_release = Event()
        second_entered = Event()
        first_client.get_state_entered = first_entered
        first_client.get_state_release = first_release
        second_client.get_state_entered = second_entered
        second_client.state = dataclasses.replace(second_client.state, thinking_level="low")
        event_id = self.model.journal.latest_id

        self.manager.request_refresh_connected()
        try:
            self.assertTrue(first_entered.wait(timeout=1))
            self.assertTrue(second_entered.wait(timeout=1))
            events = self.model.journal.wait_after(event_id, timeout=1)
            self.assertTrue(any(event.session_id == second_id for event in events))
            self.assertEqual(self.model.get(second_id)["thinking"], "low")
            self.assertEqual(self.model.get(first_id)["thinking"], "medium")
        finally:
            first_release.set()

    def test_failed_reconnect_recovers_without_browser_or_refresh_traffic(self):
        session_id = self.launch()["id"]
        FakeRpcClient.fail_get_state_once = RpcProcessExitError("RPC process exited")
        FakeRpcClient.fail_start = True

        self.manager.refresh_connected()
        self.assertEqual(self.model.get(session_id)["transport"]["status"], "disconnected")

        FakeRpcClient.fail_start = False
        deadline = time.monotonic() + 2
        while self.model.get(session_id)["transport"]["status"] != "connected" and time.monotonic() < deadline:
            time.sleep(0.01)

        self.assertEqual(self.model.get(session_id)["transport"]["status"], "connected")
        self.assertEqual(len(FakeRpcClient.instances), 3)

    def test_shutdown_cancels_pending_reconnect_without_resurrection(self):
        session_id = self.launch()["id"]
        FakeRpcClient.fail_get_state_once = RpcProcessExitError("RPC process exited")
        FakeRpcClient.fail_start = True
        self.manager.refresh_connected()
        failed_instance_count = len(FakeRpcClient.instances)

        self.manager.shutdown(session_id)
        FakeRpcClient.fail_start = False
        time.sleep(0.25)

        self.assertEqual(len(FakeRpcClient.instances), failed_instance_count)
        self.assertIsNone(self.manager._runtime_or_none(session_id))
        self.assertFalse(self.registry.load()[0]["desiredRpcRunning"])

    def test_close_cancels_pending_reconnect_without_resurrection(self):
        session_id = self.launch()["id"]
        FakeRpcClient.fail_get_state_once = RpcProcessExitError("RPC process exited")
        FakeRpcClient.fail_start = True
        self.manager.refresh_connected()
        failed_instance_count = len(FakeRpcClient.instances)

        self.manager.close()
        FakeRpcClient.fail_start = False
        time.sleep(0.25)

        self.assertEqual(len(FakeRpcClient.instances), failed_instance_count)
        self.assertIsNone(self.manager._runtime_or_none(session_id))

    def test_duplicate_reconnect_requests_share_one_retry_owner(self):
        session_id = self.launch()["id"]
        FakeRpcClient.fail_get_state_once = RpcProcessExitError("RPC process exited")
        FakeRpcClient.fail_start = True
        self.manager.refresh_connected()
        generation = self.registry.load()[0]["generation"]
        failed_instance_count = len(FakeRpcClient.instances)

        self.manager._recoveries.request(session_id, generation)
        self.manager._recoveries.request(session_id, generation)
        FakeRpcClient.fail_start = False
        deadline = time.monotonic() + 2
        while self.model.get(session_id)["transport"]["status"] != "connected" and time.monotonic() < deadline:
            time.sleep(0.01)

        self.assertEqual(self.model.get(session_id)["transport"]["status"], "connected")
        self.assertEqual(len(FakeRpcClient.instances), failed_instance_count + 1)

    def test_permanent_incompatibility_is_not_retried(self):
        session_id = self.launch()["id"]
        FakeRpcClient.fail_get_state_once = RpcProcessExitError("RPC process exited")
        original_start = FakeRpcClient.start

        def incompatible_after_original(client):
            if client.instance_number > 1:
                raise AdapterIncompatibleError("unsupported RPC protocol", kind="incompatible")
            return original_start(client)

        with patch.object(FakeRpcClient, "start", incompatible_after_original):
            self.manager.refresh_connected()
            failed_instance_count = len(FakeRpcClient.instances)
            time.sleep(0.25)

        self.assertEqual(len(FakeRpcClient.instances), failed_instance_count)
        transport = self.model.get(session_id)["transport"]
        self.assertEqual(transport["status"], "disconnected")
        self.assertIn("unsupported RPC protocol", transport["reason"])

    def test_refresh_scheduler_releases_slot_when_failure_callback_raises(self):
        calls = []
        first_failure = Event()
        second_refresh = Event()

        def refresh(session_id):
            calls.append(session_id)
            if len(calls) == 2:
                second_refresh.set()
            raise RuntimeError("refresh failed")

        def failed(session_id, reason):
            if len(calls) == 1:
                first_failure.set()
                raise RuntimeError("failure callback failed")

        scheduler = RpcRefreshScheduler(refresh, lambda *_: None, failed)
        self.addCleanup(scheduler.close)
        scheduler.request(["session"])
        self.assertTrue(first_failure.wait(timeout=1))

        scheduler.request(["session"])

        self.assertTrue(second_refresh.wait(timeout=1))

    def test_refresh_scheduler_close_is_bounded(self):
        entered = Event()
        release = Event()

        def refresh(_session_id):
            entered.set()
            release.wait()

        scheduler = RpcRefreshScheduler(refresh, lambda *_: None, lambda *_: None)
        scheduler.request(["session"])
        self.assertTrue(entered.wait(timeout=1))

        started = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, "workers did not stop"):
            scheduler.close(timeout=0.01)
        self.assertLess(time.monotonic() - started, 0.2)

        release.set()
        scheduler.close(timeout=1)

    def test_dispatch_deduplicates_and_rejects_conflicting_request_id_reuse(self):
        session_id = self.launch()["id"]
        payload = {"message": "hello"}

        first = self.manager.dispatch(session_id, "request-1", "sendMessage", payload)
        duplicate = self.manager.dispatch(session_id, "request-1", "sendMessage", dict(payload))

        self.assertEqual(duplicate, first)
        self.assertEqual([call[0] for call in FakeRpcClient.instances[-1].calls], ["prompt"])
        with self.assertRaisesRegex(ValueError, "already used for another command"):
            self.manager.dispatch(session_id, "request-1", "sendMessage", {"message": "different"})

    def test_completed_turn_refreshes_messages_after_early_acceptance(self):
        session_id = self.launch()["id"]
        client = FakeRpcClient.instances[-1]
        self.manager.dispatch(session_id, "early-image-acceptance", "sendMessage", {"message": "inspect image"})
        self.assertEqual(self.model.get(session_id)["messages"], [])

        client.state = dataclasses.replace(client.state, message_count=1)
        cursor = self.model.journal.latest_id
        client.emit_event({"type": "agent_end", "messages": []})
        deadline = time.monotonic() + 2
        while not self.model.get(session_id)["messages"] and time.monotonic() < deadline:
            events = self.model.journal.wait_after(cursor, timeout=max(0, deadline - time.monotonic()))
            if events:
                cursor = events[-1].id

        session = self.model.get(session_id)
        self.assertEqual([message["content"] for message in session["messages"]], [[{"type": "text", "text": "ready"}]])

    def test_accepted_command_is_not_failed_when_followup_snapshot_is_busy(self):
        session_id = self.launch()["id"]
        adapter = self.manager._runtime(session_id).adapter

        with patch.object(
            adapter,
            "snapshot",
            side_effect=AdapterError("state kept changing", kind="snapshot_busy"),
        ):
            result = self.manager.dispatch(session_id, "accepted-before-snapshot", "sendMessage", {"message": "hello"})

        self.assertEqual(result["status"], "accepted")
        self.assertEqual(
            [call[0] for call in FakeRpcClient.instances[-1].calls],
            ["prompt"],
        )

    def test_completed_request_is_not_replayed_after_manager_restart(self):
        session_id = self.launch()["id"]
        first = self.manager.dispatch(session_id, "durable-request", "sendMessage", {"message": "once"})
        self.manager.close()
        self.manager = RpcSessionManager(self.settings, FleetModel(), self.registry)

        duplicate = self.manager.dispatch(session_id, "durable-request", "sendMessage", {"message": "once"})

        self.assertEqual(duplicate, first)
        self.assertEqual(
            sum(call[0] == "prompt" for client in FakeRpcClient.instances for call in client.calls),
            1,
        )
        with self.assertRaises(SessionCommandConflictError):
            self.manager.dispatch(
                session_id,
                "durable-request",
                "sendMessage",
                {"message": "different"},
            )

    def test_ambiguous_transport_failure_is_not_replayed(self):
        session_id = self.launch()["id"]
        adapter = self.manager._runtime(session_id).adapter
        expected = {
            "requestId": "ambiguous-request",
            "status": "uncertain",
            "errorCode": "outcome_unknown",
        }
        with patch.object(
            adapter,
            "send_message",
            side_effect=AdapterError("slow", kind="timeout"),
        ) as send:
            first = self.manager.dispatch(
                session_id,
                "ambiguous-request",
                "sendMessage",
                {"message": "once"},
            )
            retry = self.manager.dispatch(
                session_id,
                "ambiguous-request",
                "sendMessage",
                {"message": "once"},
            )

        self.assertEqual(first, expected)
        self.assertEqual(retry, expected)
        send.assert_called_once()

    def test_disconnected_command_is_rejected_before_request_reservation(self):
        session_id = self.launch()["id"]
        self.manager.shutdown(session_id)

        with self.assertRaises(SessionNotConnectedError):
            self.manager.dispatch(
                session_id,
                "not-dispatched",
                "sendMessage",
                {"message": "never"},
            )

        self.assertFalse((self.settings.state_root / "requests.json").exists())

    def test_ui_response_is_not_blocked_by_command_or_stale_snapshot(self):
        session_id = self.launch()["id"]
        client = FakeRpcClient.instances[-1]
        branch_entered = Event()
        branch_release = Event()
        ui_response_entered = Event()
        refresh_entered = Event()
        refresh_release = Event()
        FakeRpcClient.branch_entered = branch_entered
        FakeRpcClient.branch_release = branch_release
        FakeRpcClient.ui_response_entered = ui_response_entered
        branch_result = []
        responses = []
        branch = Thread(
            target=lambda: branch_result.append(
                self.manager.dispatch(session_id, "branch-request", "branch", {"entryId": "entry-7"})
            ),
            daemon=True,
        )
        response = Thread(
            target=lambda: responses.append(
                self.manager.dispatch(
                    session_id,
                    "ui-response",
                    "respondToUi",
                    {"requestId": "branch-ui", "value": "answer"},
                )
            ),
            daemon=True,
        )
        branch.start()
        try:
            self.assertTrue(branch_entered.wait(timeout=1))
            client.emit_ui(
                type(
                    "Ui",
                    (),
                    {
                        "id": "branch-ui",
                        "method": "input",
                        "title": "Branch",
                        "message": "Name",
                        "options": None,
                        "option_details": None,
                        "placeholder": None,
                        "prefill": None,
                        "timeout": None,
                        "target_id": None,
                        "requires_response": lambda self: True,
                    },
                )()
            )
            client.get_state_entered = refresh_entered
            client.get_state_release = refresh_release
            response.start()
            self.assertTrue(ui_response_entered.wait(timeout=0.2))
            response.join(timeout=1)
            self.assertFalse(response.is_alive())
            self.assertTrue(refresh_entered.wait(timeout=1))
            branch_release.set()
            branch.join(timeout=1)
            self.assertFalse(branch.is_alive())
            self.manager.shutdown(session_id)
        finally:
            branch_release.set()
            refresh_release.set()
            response.join(timeout=1)
            branch.join(timeout=1)
            self.manager._refreshes.close(timeout=2)

        self.assertFalse(branch.is_alive())
        self.assertFalse(response.is_alive())
        self.assertEqual(branch_result[0]["status"], "accepted")
        self.assertEqual(responses[0]["status"], "accepted")
        self.assertEqual(client.calls[-1][0], "send_ui_value")
        self.assertEqual(self.model.get(session_id)["transport"]["status"], "disconnected")
        self.assertEqual(self.model.get(session_id)["ompSessionId"], "omp-branched")
        self.assertEqual(self.registry.load()[0]["ompSessionId"], "omp-branched")

    def test_dispatch_serializes_each_session_without_blocking_other_sessions(self):
        first_id = self.launch()["id"]
        second_project = self.settings.project_root / "other"
        second_project.mkdir()
        second_id = self.manager.launch("other", second_project)["id"]
        first_entered, first_release = Event(), Event()
        second_same_entered, other_entered = Event(), Event()
        FakeRpcClient.prompt_entered.update(
            {
                "first": first_entered,
                "second-same": second_same_entered,
                "other": other_entered,
            }
        )
        FakeRpcClient.prompt_release["first"] = first_release
        results = []

        def dispatch(session_id, request_id, message):
            results.append(self.manager.dispatch(session_id, request_id, "sendMessage", {"message": message}))

        first = Thread(target=dispatch, args=(first_id, "request-1", "first"))
        same = Thread(target=dispatch, args=(first_id, "request-2", "second-same"))
        other = Thread(target=dispatch, args=(second_id, "request-3", "other"))
        first.start()
        self.assertTrue(first_entered.wait(timeout=1))
        same.start()
        other.start()
        self.assertTrue(other_entered.wait(timeout=1))
        self.assertFalse(second_same_entered.wait(timeout=0.05))
        first_release.set()
        first.join(timeout=1)
        same.join(timeout=1)
        other.join(timeout=1)

        self.assertTrue(second_same_entered.is_set())
        self.assertFalse(first.is_alive())
        self.assertFalse(same.is_alive())
        self.assertFalse(other.is_alive())
        self.assertEqual(len(results), 3)

    def test_failed_launch_is_persisted_as_not_desired_running(self):
        FakeRpcClient.fail_start = True
        with self.assertRaisesRegex(RuntimeError, "launch failed"):
            self.launch()

        record = self.registry.load()[0]
        self.assertEqual(record["id"], "deck-stable-id")
        self.assertFalse(record["desiredRpcRunning"])
        self.assertTrue(FakeRpcClient.instances[-1].stopped)
        transport = self.model.get(record["id"])["transport"]
        self.assertEqual(transport["status"], "disconnected")
        self.assertEqual(transport["reason"], "launch failed: launch failed")

    def test_failed_launch_cleanup_retains_runtime_until_stop_succeeds(self):
        FakeRpcClient.fail_start = True
        FakeRpcClient.fail_stop = True

        with self.assertRaisesRegex(RuntimeError, "RPC cleanup failed"):
            self.launch()

        self.assertIsNotNone(self.manager._runtime_or_none("deck-stable-id"))
        FakeRpcClient.fail_stop = False
        self.manager.close()
        self.assertIsNone(self.manager._runtime_or_none("deck-stable-id"))


if __name__ == "__main__":
    unittest.main()
