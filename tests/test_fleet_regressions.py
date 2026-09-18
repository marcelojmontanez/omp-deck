import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

from omp_deck.fleet_model import FleetModel
from omp_deck.read_cursors import CursorStore
from tests import test_fleet_model as fixtures


class DelayedCursorStore(CursorStore):
    def __init__(self, path):
        super().__init__(path)
        self.delay = False
        self.entered = threading.Event()
        self.release = threading.Event()

    def last_read(self, session_id):
        value = super().last_read(session_id)
        if self.delay:
            self.delay = False
            self.entered.set()
            if not self.release.wait(3):
                raise TimeoutError("cursor fixture was not released")
        return value


class FleetRegressionTests(unittest.TestCase):
    def setUp(self):
        self.fixtures = fixtures.FleetModelTests()

    def connect(self, model, snapshot=None):
        model.load_record(self.fixtures.make_record())
        model.apply_rpc_snapshot(
            "deck-1",
            instance_id="instance-a",
            generation=3,
            snapshot=snapshot or self.fixtures.make_snapshot(),
        )
        return model

    def apply(self, model, sequence, kind, payload):
        return model.apply_adapter_event(
            "deck-1",
            instance_id="instance-a",
            generation=3,
            event=self.fixtures.event(sequence, kind, payload),
        )

    def test_snapshot_cannot_rewind_an_applied_event_or_reopen_its_sequence(self):
        snapshot = self.fixtures.make_snapshot(sequence=10, is_streaming=False)
        model = self.connect(FleetModel(), snapshot)
        self.assertTrue(self.apply(model, 11, "agent_start", {}))
        revision = model.snapshot()["snapshotGeneration"]
        self.assertFalse(
            model.apply_rpc_snapshot(
                "deck-1",
                instance_id="instance-a",
                generation=3,
                snapshot=snapshot,
            )
        )
        self.assertEqual(model.get("deck-1")["state"], "working")
        self.assertEqual(model.snapshot()["snapshotGeneration"], revision)
        self.assertFalse(self.apply(model, 11, "agent_start", {}))

    def test_cursor_storage_does_not_block_another_session(self):
        with tempfile.TemporaryDirectory() as directory:
            store = DelayedCursorStore(Path(directory) / "cursors.json")
            model = self.connect(FleetModel(cursor_store=store))
            model.load_record(self.fixtures.make_record(id="other"))
            store.delay = True
            with ThreadPoolExecutor(max_workers=2) as pool:
                update = pool.submit(
                    model.apply_rpc_snapshot,
                    "deck-1",
                    instance_id="instance-a",
                    generation=3,
                    snapshot=self.fixtures.make_snapshot(sequence=1),
                )
                try:
                    self.assertTrue(store.entered.wait(1))
                    independent = pool.submit(model.get, "other")
                    self.assertEqual(independent.result(timeout=1)["id"], "other")
                finally:
                    store.release.set()
                self.assertTrue(update.result(timeout=1))

    def test_completed_mark_read_wins_over_an_older_cursor_read(self):
        with tempfile.TemporaryDirectory() as directory:
            store = DelayedCursorStore(Path(directory) / "cursors.json")
            snapshot = replace(
                self.fixtures.make_snapshot(), messages=({"id": "answer", "role": "assistant", "content": "Finished"},)
            )
            model = self.connect(FleetModel(cursor_store=store), snapshot)
            self.assertTrue(model.get("deck-1")["unread"])
            store.delay = True
            with ThreadPoolExecutor(max_workers=1) as pool:
                update = pool.submit(
                    model.apply_rpc_snapshot,
                    "deck-1",
                    instance_id="instance-a",
                    generation=3,
                    snapshot=replace(snapshot, sequence=1),
                )
                try:
                    self.assertTrue(store.entered.wait(1))
                    self.assertFalse(model.mark_read("deck-1")["unread"])
                finally:
                    store.release.set()
                self.assertTrue(update.result(timeout=1))
            self.assertFalse(model.get("deck-1")["unread"])
            self.assertEqual(store.last_read("deck-1"), "answer")

    def test_streaming_message_patches_settle_into_one_canonical_message(self):
        snapshot = self.fixtures.make_snapshot()
        model = self.connect(FleetModel(), snapshot)
        partial = {"role": "assistant", "timestamp": 1700000000000, "content": [{"type": "text", "text": "H"}]}
        self.apply(model, 1, "message_start", {"message": partial})
        stream_id = model.get("deck-1")["streamingMessage"]["id"]
        complete = {**partial, "content": [{"type": "text", "text": "Hello"}]}
        self.apply(model, 2, "message_update", {"message": complete})
        event = model.journal.replay_after(model.journal.latest_id - 1)[0]
        patch = event.payload["sessionPatch"]
        self.assertEqual(patch["streamingMessage"]["content"], complete["content"])
        self.assertEqual(patch["streamingMessage"]["id"], stream_id)
        self.assertNotIn("messages", patch)
        self.assertEqual(len(model.get("deck-1")["messages"]), 1)
        self.apply(model, 3, "message_end", {"message": complete})
        settled = model.get("deck-1")
        self.assertIsNone(settled["streamingMessage"])
        self.assertEqual(settled["messages"][-1]["content"], complete["content"])
        message_id = settled["messages"][-1]["id"]
        self.assertTrue(settled["unread"])
        self.apply(model, 4, "agent_end", {"messages": [complete]})
        model.apply_rpc_snapshot(
            "deck-1",
            instance_id="instance-a",
            generation=3,
            snapshot=replace(snapshot, messages=(*snapshot.messages, complete), sequence=4),
        )
        self.assertEqual([message["id"] for message in model.get("deck-1")["messages"]].count(message_id), 1)

    def test_midstream_snapshot_of_settled_history_preserves_active_text(self):
        snapshot = self.fixtures.make_snapshot()
        model = self.connect(FleetModel(), snapshot)
        partial = {"role": "assistant", "timestamp": 1700000000000, "content": "Partial response"}
        self.apply(model, 1, "message_start", {"message": partial})
        visible = model.get("deck-1")["streamingMessage"]
        model.apply_rpc_snapshot(
            "deck-1",
            instance_id="instance-a",
            generation=3,
            snapshot=replace(snapshot, sequence=1),
        )
        self.assertEqual(model.get("deck-1")["streamingMessage"], visible)
        self.apply(model, 2, "message_end", {"message": partial})
        self.assertIsNone(model.get("deck-1")["streamingMessage"])
        self.assertEqual(model.get("deck-1")["messages"][-1]["content"], visible["content"])

    def test_transport_patch_moves_retired_session_to_history(self):
        model = self.connect(FleetModel())
        model.load_record(self.fixtures.make_record(desiredRpcRunning=False))
        self.assertEqual(model.get("deck-1")["lifecycle"], "ongoing")
        model.set_transport(
            "deck-1",
            instance_id=None,
            generation=3,
            status="disconnected",
            reason="closed",
        )
        patch = model.journal.replay_after(model.journal.latest_id - 1)[0].payload["sessionPatch"]
        self.assertEqual(patch["lifecycle"], "history")
        self.assertEqual(model.get("deck-1")["lifecycle"], "history")

    def test_subagent_refresh_failure_is_visible_without_transport_degradation(self):
        model = self.connect(FleetModel())
        self.apply(model, 1, "subagent_refresh_failed", {"error": "private native detail"})
        session = model.get("deck-1")
        self.assertIsNotNone(session["subagentError"])
        self.assertNotIn("private native detail", session["subagentError"])
        self.assertEqual(session["transport"]["status"], "connected")
        self.assertEqual(session["state"], "working")
        self.apply(model, 2, "subagents", {"subagents": [{"id": "recovered", "status": "completed"}]})
        self.assertIsNone(model.get("deck-1")["subagentError"])
        self.assertEqual(model.get("deck-1")["subagents"], [{"id": "recovered", "status": "completed"}])

    def test_native_failure_remains_visible_without_inventing_a_deck_receipt(self):
        model = self.connect(FleetModel())
        self.apply(
            model,
            1,
            "native_delivery_failed",
            {
                "nativeRequestId": "native-17",
                "errorCode": "command",
                "error": "private native detail",
            },
        )
        error = model.get("deck-1")["deliveryError"]
        self.assertEqual(error["nativeRequestId"], "native-17")
        self.assertFalse(error["correlationAvailable"])
        self.assertNotIn("requestId", error)
        self.assertNotIn("private native detail", str(error))
        model.apply_rpc_snapshot(
            "deck-1",
            instance_id="instance-a",
            generation=3,
            snapshot=self.fixtures.make_snapshot(sequence=1),
        )
        self.assertEqual(model.get("deck-1")["deliveryError"], error)
