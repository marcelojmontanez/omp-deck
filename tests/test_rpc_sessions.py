from __future__ import annotations

import dataclasses
import enum
import tempfile
import time
import unittest
from pathlib import Path
from threading import Event, Thread
from unittest.mock import patch

from omp_rpc import ReadyEvent, RpcProcessExitError

from omp_deck.fleet_model import FleetModel
from omp_deck.registry_store import RegistryStore
from omp_deck.rpc_adapter import AdapterError
from omp_deck.rpc_refresh import RpcRefreshScheduler
from omp_deck.rpc_sessions import (
    RpcSessionManager,
    SessionCommandConflictError,
    SessionNotConnectedError,
)
from omp_deck.settings import Settings


@dataclasses.dataclass(frozen=True)
class _OfficialState:
    session_id: str = "omp-official-1"
    session_file: str = "/sessions/omp-official-1.jsonl"
    is_streaming: bool = False
    is_compacting: bool = False
    message_count: int = 1
    queued_message_count: int = 0
    thinking_level: str = "medium"


class _Outcome(enum.Enum):
    COMPLETE = "complete"


@dataclasses.dataclass(frozen=True)
class _Correlation:
    session_id: str
    outcome: _Outcome


class FakeRpcClient:
    instances: list[FakeRpcClient] = []
    fail_start = False
    fail_stop = False

    fail_get_state_once: BaseException | None = None
    prompt_entered: dict[str, Event] = {}
    prompt_release: dict[str, Event] = {}

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.listeners = {kind: [] for kind in ("ready", "event", "ui", "protocol", "extension")}
        self.calls = []
        self.stopped = False
        self.state = _OfficialState()
        type(self).instances.append(self)
        self.get_state_entered: Event | None = None
        self.get_state_release: Event | None = None

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

    def start(self):
        if type(self).fail_start:
            raise RuntimeError("launch failed")
        ready = ReadyEvent(protocol_version=1, supported_protocol_versions=(1, 2))
        for listener in tuple(self.listeners["ready"]):
            listener(ready)
        return self

    def stop(self):
        self.stopped = True
        if type(self).fail_stop:
            raise RuntimeError("stop failed")

    def get_state(self):
        if self.get_state_entered is not None:
            self.get_state_entered.set()
        if self.get_state_release is not None:
            self.get_state_release.wait(timeout=2)
        failure = type(self).fail_get_state_once
        if failure is not None:
            type(self).fail_get_state_once = None
            raise failure
        return self.state

    def get_messages(self):
        return ({"id": "message-1", "role": "assistant", "content": "ready"},)

    def get_todos(self):
        return ()

    def get_subagents(self):
        return ()

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

    def branch(self, *args):
        self._call("branch", *args)
        self.state = dataclasses.replace(
            self.state, session_id="omp-branched", session_file="/sessions/omp-branched.jsonl"
        )
        return _Correlation("omp-branched", _Outcome.COMPLETE)

    def switch_session(self, *args):
        self._call("switch_session", *args)
        self.state = dataclasses.replace(
            self.state, session_id="omp-switched", session_file="/sessions/omp-switched.jsonl"
        )
        return _Correlation("omp-switched", _Outcome.COMPLETE)

    def compact(self, *args):
        self._call("compact", *args)
        return _Outcome.COMPLETE

    def send_ui_value(self, *args):
        return self._call("send_ui_value", *args)

    def send_ui_confirmation(self, *args):
        return self._call("send_ui_confirmation", *args)

    def cancel_ui_request(self, *args, **kwargs):
        return self._call("cancel_ui_request", *args, **kwargs)

    def emit_event(self, value):
        for listener in tuple(self.listeners["event"]):
            listener(value)


class TestRpcSessionManager(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
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
        FakeRpcClient.fail_get_state_once = None
        FakeRpcClient.prompt_entered.clear()
        FakeRpcClient.prompt_release.clear()
        self.rpc_patch = patch("omp_deck.rpc_sessions.RpcClient", FakeRpcClient)
        self.rpc_patch.start()
        self.manager = RpcSessionManager(self.settings, self.model, self.registry)

    def tearDown(self):
        self.manager.close()
        self.rpc_patch.stop()
        self.temporary.cleanup()

    def launch(self):
        return self.manager.launch("project", self.project, title="RPC test")

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
        self.assertEqual(record["recoveryMetadata"], {"sessionFile": "/sessions/omp-official-1.jsonl"})
        self.assertTrue(record["desiredRpcRunning"])
        self.assertEqual(session["transport"]["status"], "connected")

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

    def test_commands_map_to_official_calls_and_plain_correlation_results(self):
        session_id = self.launch()["id"]
        client = FakeRpcClient.instances[-1]
        commands = (
            ("sendMessage", {"message": "hello", "images": [{"data": "x"}], "streamingBehavior": "steer"}),
            ("followUp", {"message": "later", "images": []}),
            ("interrupt", {}),
            ("setModel", {"provider": "openai", "modelId": "gpt"}),
            ("setThinking", {"level": "high"}),
            ("branch", {"entryId": "entry-7"}),
            ("switchSession", {"sessionPath": "/sessions/other.jsonl"}),
            ("compact", {"customInstructions": "short"}),
        )
        results = {action: self.manager.command(session_id, action, payload) for action, payload in commands}

        self.assertEqual(
            [call[0] for call in client.calls],
            [
                "prompt",
                "follow_up",
                "abort",
                "set_model",
                "set_thinking_level",
                "branch",
                "switch_session",
                "compact",
            ],
        )
        self.assertEqual(client.calls[0][2]["streaming_behavior"], "steer")
        self.assertEqual(results["setModel"]["result"], {"session_id": "omp-official-1", "outcome": "complete"})
        self.assertEqual(results["branch"]["result"]["session_id"], "omp-branched")
        self.assertEqual(results["switchSession"]["result"]["session_id"], "omp-switched")
        self.assertEqual(results["compact"]["result"], "complete")
        self.assertTrue(all(result["accepted"] for result in results.values()))

    def test_shutdown_clears_desired_state_and_disconnects(self):
        session_id = self.launch()["id"]
        client = FakeRpcClient.instances[-1]
        result = self.manager.command(session_id, "shutdown", {})

        self.assertEqual(result, {"accepted": True, "result": None})
        self.assertTrue(client.stopped)
        self.assertFalse(self.registry.load()[0]["desiredRpcRunning"])
        self.assertEqual(self.model.get(session_id)["transport"]["status"], "disconnected")
        self.assertEqual(self.model.get(session_id)["transport"]["reason"], "shut down")

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

    def test_startup_recovery_resumes_official_id_in_original_project(self):
        original = self.launch()
        self.manager.close()
        recovering_model = FleetModel()
        recovering = RpcSessionManager(self.settings, recovering_model, self.registry)
        self.addCleanup(recovering.close)

        self.assertEqual(recovering.recover_desired(), {})
        client = FakeRpcClient.instances[-1]
        self.assertEqual(client.kwargs["extra_args"], ("--resume", "omp-official-1"))
        self.assertEqual(client.kwargs["cwd"], str(self.project))
        self.assertEqual(recovering_model.get(original["id"])["id"], "deck-stable-id")
        self.assertEqual(recovering_model.get(original["id"])["ompSessionId"], "omp-official-1")

    def test_branch_identity_is_persisted_and_recovered(self):
        session_id = self.launch()["id"]
        self.manager.command(session_id, "branch", {"entryId": "entry-7"})
        record = self.registry.load()[0]
        self.assertEqual(record["ompSessionId"], "omp-branched")
        self.assertEqual(record["recoveryMetadata"], {"sessionFile": "/sessions/omp-branched.jsonl"})

        self.manager.close()
        recovering = RpcSessionManager(self.settings, FleetModel(), self.registry)
        self.addCleanup(recovering.close)
        self.assertEqual(recovering.recover_desired(), {})
        self.assertEqual(
            FakeRpcClient.instances[-1].kwargs["extra_args"],
            ("--resume", "omp-branched"),
        )

    def test_switch_identity_is_persisted_and_recovered(self):
        session_id = self.launch()["id"]
        self.manager.command(session_id, "switchSession", {"sessionPath": "/sessions/other.jsonl"})
        record = self.registry.load()[0]
        self.assertEqual(record["ompSessionId"], "omp-switched")
        self.assertEqual(record["recoveryMetadata"], {"sessionFile": "/sessions/omp-switched.jsonl"})

        self.manager.close()
        recovering = RpcSessionManager(self.settings, FleetModel(), self.registry)
        self.addCleanup(recovering.close)
        self.assertEqual(recovering.recover_desired(), {})
        self.assertEqual(
            FakeRpcClient.instances[-1].kwargs["extra_args"],
            ("--resume", "omp-switched"),
        )

    def test_repeated_recovery_does_not_mutate_generation_or_spawn_again(self):
        session_id = self.launch()["id"]
        self.manager.close()
        recovering_model = FleetModel()
        recovering = RpcSessionManager(self.settings, recovering_model, self.registry)
        self.addCleanup(recovering.close)

        self.assertEqual(recovering.recover_desired(), {})
        generation = self.registry.load()[0]["generation"]
        child_count = len(FakeRpcClient.instances)
        self.assertEqual(recovering.recover_desired(), {})

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

        self.assertEqual(recovering.recover_desired(), {session_id: "launch failed"})
        transport = recovering_model.get(session_id)["transport"]
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
        self.assertEqual(replacement.kwargs["extra_args"], ("--resume", "omp-official-1"))
        self.assertEqual(self.model.get(session_id)["id"], session_id)
        self.assertEqual(
            self.model.get(session_id)["transport"]["status"],
            "connected",
            self.model.snapshot(),
        )

    def test_background_refresh_isolates_a_blocked_session(self):
        first_id = self.launch()["id"]
        first_client = FakeRpcClient.instances[-1]
        second_project = self.settings.project_root / "second"
        second_project.mkdir()
        second_id = self.manager.launch("second", second_project)["id"]
        second_client = FakeRpcClient.instances[-1]
        first_client.get_state_entered = Event()
        first_client.get_state_release = Event()
        second_client.get_state_entered = Event()
        second_client.state = dataclasses.replace(second_client.state, thinking_level="low")
        event_id = self.model.journal.latest_id

        self.manager.request_refresh_connected()

        self.assertTrue(first_client.get_state_entered.wait(timeout=1))
        self.assertTrue(second_client.get_state_entered.wait(timeout=1))
        events = self.model.journal.wait_after(event_id, timeout=1)
        self.assertTrue(any(event.session_id == second_id for event in events))
        self.assertEqual(self.model.get(second_id)["thinking"], "low")
        self.assertEqual(self.model.get(first_id)["thinking"], "medium")
        first_client.get_state_release.set()

    def test_failed_reconnect_is_retried_on_next_connected_refresh(self):
        session_id = self.launch()["id"]
        FakeRpcClient.fail_get_state_once = RpcProcessExitError("RPC process exited")
        FakeRpcClient.fail_start = True

        self.manager.refresh_connected()
        self.assertEqual(self.model.get(session_id)["transport"]["status"], "disconnected")

        FakeRpcClient.fail_start = False
        self.manager.request_refresh_connected()
        deadline = time.monotonic() + 2
        while self.model.get(session_id)["transport"]["status"] != "connected" and time.monotonic() < deadline:
            time.sleep(0.01)

        self.assertEqual(self.model.get(session_id)["transport"]["status"], "connected")
        self.assertEqual(len(FakeRpcClient.instances), 3)

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
