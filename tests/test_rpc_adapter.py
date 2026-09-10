from __future__ import annotations

import unittest
from dataclasses import dataclass

from omp_rpc import ReadyEvent, RpcProcessExitError, RpcTimeoutError

from omp_deck.rpc_adapter import (
    AdapterDisconnectedError,
    AdapterIncompatibleError,
    AdapterTimeoutError,
    SessionAdapter,
)


@dataclass(frozen=True)
class FakeUiRequest:
    id: str
    method: str
    title: str | None = None
    options: tuple[str, ...] | None = None
    message: str | None = None
    option_details: tuple[dict[str, object], ...] | None = None
    placeholder: str | None = None
    prefill: str | None = None
    timeout: int | None = None
    target_id: str | None = None

    def requires_response(self) -> bool:
        return self.method in {"select", "confirm", "input", "editor"}


@dataclass(frozen=True)
class FakeState:
    session_id: str
    is_streaming: bool


@dataclass(frozen=True)
class FakeTodo:
    id: str
    name: str
    tasks: tuple


class FakeClient:
    def __init__(self, ready: ReadyEvent | None = None) -> None:
        self.ready = ready or ReadyEvent(protocol_version=1, supported_protocol_versions=(1, 2))
        self.listeners: dict[str, list] = {"ready": [], "event": [], "ui": [], "protocol": [], "extension": []}
        self.calls: list[tuple] = []
        self.stopped = False
        self.unsubscribed = 0
        self.state_reads = 0
        self.on_state_read = None
        self.state = FakeState(session_id="omp-1", is_streaming=False)
        self.messages = ({"id": "m1", "role": "user", "content": "hello"},)
        self.todos = (FakeTodo(id="phase", name="Now", tasks=()),)
        self.subagents = ({"id": "sub-1", "status": "running"},)
        self.failure: BaseException | None = None

    def _on(self, kind, listener):
        self.listeners[kind].append(listener)

        def unsubscribe():
            self.listeners[kind].remove(listener)
            self.unsubscribed += 1

        return unsubscribe

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
        for listener in tuple(self.listeners["ready"]):
            listener(self.ready)
        return self

    def stop(self):
        self.stopped = True

    def get_state(self):
        self.state_reads += 1
        if self.on_state_read is not None:
            self.on_state_read(self.state_reads)
        return self.state

    def get_messages(self):
        return self.messages

    def get_todos(self):
        return self.todos

    def get_subagents(self):
        return self.subagents

    def _call(self, name, *args, **kwargs):
        if self.failure:
            raise self.failure
        self.calls.append((name, args, kwargs))

    def prompt(self, *args, **kwargs):
        return self._call("prompt", *args, **kwargs)

    def follow_up(self, *args, **kwargs):
        return self._call("follow_up", *args, **kwargs)

    def abort(self):
        return self._call("abort")

    def set_model(self, *args):
        return self._call("set_model", *args)

    def set_thinking_level(self, *args):
        return self._call("set_thinking_level", *args)

    def new_session(self):
        return self._call("new_session")

    def branch(self, *args):
        return self._call("branch", *args)

    def switch_session(self, *args):
        return self._call("switch_session", *args)

    def compact(self, *args):
        return self._call("compact", *args)

    def send_ui_value(self, *args):
        return self._call("send_ui_value", *args)

    def send_ui_confirmation(self, *args):
        return self._call("send_ui_confirmation", *args)

    def cancel_ui_request(self, *args, **kwargs):
        return self._call("cancel_ui_request", *args, **kwargs)

    def emit(self, kind, value):
        for listener in tuple(self.listeners[kind]):
            listener(value)


def started(client=None, **kwargs):
    client = client or FakeClient()
    adapter = SessionAdapter(lambda: client, **kwargs)
    adapter.start()
    return adapter, client


class TestRpcAdapter(unittest.TestCase):
    def test_start_snapshot_and_close_use_injected_official_shape(self):
        observed = []
        adapter, client = started(on_event=observed.append)
        snapshot = adapter.snapshot()
        self.assertEqual(snapshot.transport_status, "connected")
        self.assertEqual(snapshot.state["session_id"], "omp-1")
        self.assertEqual(snapshot.messages[0]["id"], "m1")
        self.assertEqual(snapshot.todos[0]["id"], "phase")
        self.assertEqual(snapshot.subagents[0]["id"], "sub-1")
        adapter.close()
        self.assertTrue(client.stopped)
        self.assertEqual(adapter.transport_status, "disconnected")
        self.assertEqual(client.unsubscribed, 5)
        self.assertTrue(all(not listeners for listeners in client.listeners.values()))
        callback_count = len(observed)
        client.emit("event", {"type": "after_close"})
        self.assertEqual(len(observed), callback_count)

    def test_snapshot_retries_when_event_crosses_capture_and_watermark_matches(self):
        adapter, client = started()

        def emit_during_first_capture(read_number):
            if read_number == 1:
                client.emit("event", {"type": "state_changed"})

        client.on_state_read = emit_during_first_capture
        snapshot = adapter.snapshot()
        crossed_event = adapter.drain_events()[-1]
        self.assertEqual(client.state_reads, 2)
        self.assertEqual(crossed_event.type, "state_changed")
        self.assertEqual(snapshot.sequence, crossed_event.sequence)

    def test_start_rejects_unadvertised_protocol_and_stops_client(self):
        client = FakeClient(ReadyEvent(protocol_version=9, supported_protocol_versions=(9,)))
        adapter = SessionAdapter(lambda: client)
        with self.assertRaises(AdapterIncompatibleError):
            adapter.start()
        self.assertTrue(client.stopped)
        self.assertEqual(client.unsubscribed, 5)
        self.assertTrue(all(not listeners for listeners in client.listeners.values()))

    def test_commands_map_only_to_public_client_methods(self):
        adapter, client = started()
        adapter.send_message("one", images=[{"data": "x"}], streaming_behavior="steer")
        adapter.follow_up("two")
        adapter.interrupt()
        adapter.set_model("openai", "gpt")
        adapter.set_thinking("high")
        adapter.new_session()
        adapter.branch("entry")
        adapter.switch("session.jsonl")
        adapter.resume("other.jsonl")
        self.assertEqual(
            [call[0] for call in client.calls],
            [
                "prompt",
                "follow_up",
                "abort",
                "set_model",
                "set_thinking_level",
                "new_session",
                "branch",
                "switch_session",
                "switch_session",
            ],
        )
        self.assertEqual(client.calls[0][2]["streaming_behavior"], "steer")

    def test_bounded_event_ingestion_is_ordered_and_callbacks_are_outside_storage(self):
        observed = []
        adapter, client = started(event_limit=2, on_event=observed.append)
        client.emit("event", {"type": "turn_start", "turn": 1})
        client.emit("event", {"type": "turn_end", "turn": 1})
        client.emit("event", {"type": "agent_end"})
        events = adapter.drain_events()
        self.assertEqual([event.type for event in events], ["turn_end", "agent_end"])
        self.assertEqual(events[1].sequence, events[0].sequence + 1)
        self.assertEqual(observed[-1], events[-1])
        self.assertEqual(adapter.drain_events(), ())

    def test_ui_projection_preserves_ids_options_descriptions_custom_and_first_response(self):
        now = [100.0]
        adapter, client = started(clock=lambda: now[0])
        client.emit(
            "ui",
            FakeUiRequest(
                id="ask-7",
                method="select",
                title="Pick",
                message="Choose",
                options=("a", "b"),
                option_details=({"label": "Alpha", "description": "Recommended"}, {"label": "Beta"}),
                placeholder="custom",
                prefill="typed already",
                timeout=5000,
            ),
        )
        pending = adapter.pending_ui_request()
        self.assertIsNotNone(pending)
        self.assertEqual(pending.id, "ask-7")
        self.assertEqual(pending.options[0].value, "a")
        self.assertEqual(pending.options[0].label, "Alpha")
        self.assertEqual(pending.options[0].description, "Recommended")
        self.assertEqual(pending.expires_at, 105.0)
        self.assertEqual(pending.prefill, "typed already")
        self.assertEqual(adapter.respond_ui("ask-7", "b"), "accepted")
        self.assertEqual(adapter.respond_ui("ask-7", "a"), "stale")
        self.assertEqual(client.calls[-1], ("send_ui_value", ("ask-7", "b"), {}))

    def test_failed_ui_delivery_remains_pending_for_retry(self):
        adapter, client = started()
        client.emit("ui", FakeUiRequest(id="retry", method="select", options=("a", "b")))
        client.failure = RpcTimeoutError("slow")

        with self.assertRaises(AdapterTimeoutError):
            adapter.respond_ui("retry", "b")

        self.assertEqual(adapter.pending_ui_request().id, "retry")
        client.failure = None
        self.assertEqual(adapter.respond_ui("retry", "b"), "accepted")

    def test_failed_ui_cancellation_remains_pending_for_retry(self):
        adapter, client = started()
        client.emit("ui", FakeUiRequest(id="retry-cancel", method="input"))
        client.failure = RpcTimeoutError("slow")

        with self.assertRaises(AdapterTimeoutError):
            adapter.cancel_ui("retry-cancel")

        self.assertEqual(adapter.pending_ui_request().id, "retry-cancel")
        client.failure = None
        self.assertEqual(adapter.cancel_ui("retry-cancel"), "cancelled")

    def test_confirm_shape_cancel_notification_and_timeout_are_exact(self):
        now = [10.0]
        adapter, client = started(clock=lambda: now[0])
        client.emit("ui", FakeUiRequest(id="confirm", method="confirm", timeout=1000))
        self.assertEqual(adapter.respond_ui("confirm", True), "accepted")
        self.assertEqual(client.calls[-1], ("send_ui_confirmation", ("confirm", True), {}))
        client.emit("ui", FakeUiRequest(id="input", method="input", placeholder="Value", timeout=1000))
        client.emit("ui", FakeUiRequest(id="cancel-frame", method="cancel", target_id="input"))
        self.assertEqual(adapter.cancel_ui("input"), "stale")
        client.emit("ui", FakeUiRequest(id="late", method="editor", timeout=1000))
        now[0] = 12.0
        self.assertEqual(adapter.respond_ui("late", "text"), "stale")
        self.assertEqual(client.calls[-1], ("cancel_ui_request", ("late",), {"timed_out": True}))

    def test_passive_ui_is_an_event_not_a_pending_request(self):
        adapter, client = started()
        client.emit("ui", FakeUiRequest(id="notice", method="notify", message="hello"))
        self.assertIsNone(adapter.pending_ui_request())
        self.assertEqual(adapter.drain_events()[-1].payload["id"], "notice")

    def test_protocol_error_is_emitted_without_disconnecting_transport(self):
        adapter, client = started()
        client.emit("protocol", ValueError("malformed notification"))
        event = adapter.drain_events()[-1]
        self.assertEqual(event.type, "protocol_error")
        self.assertEqual(event.payload, {"error": "malformed notification"})
        self.assertEqual(adapter.transport_status, "connected")
        self.assertIsNone(adapter.reason)

    def test_extension_error_is_emitted(self):
        adapter, client = started()
        client.emit("extension", {"extension": "ui", "error": "render failed"})
        event = adapter.drain_events()[-1]
        self.assertEqual(event.type, "extension_error")
        self.assertEqual(event.payload, {"extension": "ui", "error": "render failed"})

    def test_transport_failures_remain_distinguishable(self):
        cases = [
            (RpcTimeoutError("slow"), AdapterTimeoutError, "timeout"),
            (RpcProcessExitError("gone"), AdapterDisconnectedError, "process_exit"),
        ]
        for failure, expected, kind in cases:
            with self.subTest(failure=type(failure).__name__):
                adapter, client = started()
                client.failure = failure
                with self.assertRaises(expected) as caught:
                    adapter.interrupt()
                self.assertEqual(caught.exception.kind, kind)
