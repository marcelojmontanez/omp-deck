import base64
import hashlib
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime

from omp_deck.event_stream import EventJournal, ReplayUnavailableError
from omp_deck.fleet_model import FleetModel
from omp_deck.rpc_adapter import AdapterEvent, AdapterSnapshot


class FleetModelTests(unittest.TestCase):
    def make_record(self, **overrides):
        record = {
            "id": "deck-1",
            "title": "Build Deck",
            "displayName": None,
            "pinned": False,
            "presentationRevision": 0,
            "project": {"name": "deck", "path": "/work/deck"},
            "adapterKind": "rpc",
            "desiredRpcRunning": True,
            "ompSessionId": "omp-old",
            "generation": 3,
            "updatedAt": "2026-09-03T12:00:00+00:00",
        }
        record.update(overrides)
        return record

    def make_snapshot(self, sequence=0, **state_overrides):
        state = {
            "session_id": "omp-live",
            "is_streaming": True,
            "is_compacting": False,
            "model": {
                "provider": "openai",
                "id": "gpt-5",
                "name": "GPT 5",
                "api": "responses",
                "reasoning": True,
                "thinking": {"mode": "effort", "efforts": ("low", "medium", "high", "xhigh")},
            },
            "thinking_level": "high",
            "message_count": 7,
            "queued_message_count": 2,
        }
        state.update(state_overrides)
        return AdapterSnapshot(
            state=state,
            messages=({"role": "user", "content": "hello"},),
            todos=({"content": "ship", "status": "pending"},),
            subagents=({"id": "agent-1", "status": "running"},),
            sequence=sequence,
            pending_ui_request=None,
            transport_status="connected",
            reason=None,
        )

    @staticmethod
    def event(sequence, event_type="message_update", payload=None):
        return AdapterEvent(
            sequence=sequence,
            type=event_type,
            payload=payload or {"delta": "x"},
            timestamp=float(sequence),
        )

    def connected_model(self):
        model = FleetModel()
        model.load_record(self.make_record())
        self.assertTrue(
            model.apply_rpc_snapshot("deck-1", instance_id="instance-a", generation=3, snapshot=self.make_snapshot())
        )
        return model

    def test_inline_native_images_project_private_blob_links_without_base64(self):
        encoded = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aM1sAAAAASUVORK5CYII="
        digest = hashlib.sha256(base64.b64decode(encoded)).hexdigest()
        model = self.connected_model()
        snapshot = replace(
            self.make_snapshot(sequence=1),
            messages=(
                {
                    "role": "user",
                    "content": [
                        {"type": "image", "data": encoded, "mimeType": "image/png"},
                        {"type": "image", "data": encoded, "mimeType": "image/jpeg"},
                    ],
                },
            ),
        )
        model.apply_rpc_snapshot("deck-1", instance_id="instance-a", generation=3, snapshot=snapshot)
        self.assertEqual(
            model.get("deck-1")["messages"][0]["content"],
            [{"type": "image", "url": f"/api/blob/{digest}"}, {"type": "image"}],
        )

    def test_rpc_todos_follow_structured_tool_results(self):
        model = self.connected_model()
        phases = [{"name": "Release", "tasks": [{"content": "ship", "status": "completed"}]}]

        def update(sequence, phases, tool_name="todo"):
            model.apply_adapter_event(
                "deck-1",
                instance_id="instance-a",
                generation=3,
                event=self.event(
                    sequence,
                    "tool_execution_end",
                    {"tool_name": tool_name, "result": {"details": {"phases": phases, "storage": "session"}}},
                ),
            )

        update(1, phases)
        completed = [{"content": "ship", "status": "completed", "phase": "Release"}]
        self.assertEqual(model.get("deck-1")["todos"], completed)
        update(2, [], tool_name="read")
        update(3, "malformed")
        update(1, [])
        self.assertEqual(model.get("deck-1")["todos"], completed)
        update(4, [])
        self.assertEqual(model.get("deck-1")["todos"], [])

    def test_rpc_snapshot_is_normalized_for_public_consumers(self):
        model = self.connected_model()

        session = model.get("deck-1")

        self.assertEqual(
            set(session),
            {
                "id",
                "ompSessionId",
                "title",
                "displayName",
                "pinned",
                "project",
                "lifecycle",
                "state",
                "transport",
                "historyError",
                "capabilities",
                "attachmentLimits",
                "messages",
                "streamingMessage",
                "deliveryError",
                "todos",
                "subagents",
                "subagentError",
                "pendingUiRequest",
                "unread",
                "updatedAt",
                "workingOn",
                "model",
                "thinking",
                "thinkingLevels",
            },
        )
        self.assertEqual(session["project"], {"name": "deck", "path": "/work/deck"})
        self.assertEqual(
            session["transport"],
            {
                "kind": "rpc",
                "status": "connected",
                "label": "Managed RPC",
                "instanceId": "instance-a",
                "reason": None,
            },
        )
        self.assertIsNone(session["historyError"])
        self.assertEqual(session["ompSessionId"], "omp-live")
        self.assertEqual(session["state"], "working")
        self.assertTrue(session["capabilities"]["attachments"])
        self.assertEqual(session["attachmentLimits"], {"maxImages": 4, "maxTotalBytes": 20 * 1024 * 1024})
        self.assertEqual(
            session["model"],
            {"provider": "openai", "id": "gpt-5", "name": "GPT 5", "api": "responses"},
        )
        self.assertEqual(session["thinking"], "high")
        self.assertEqual(session["thinkingLevels"], ["off", "low", "medium", "high", "xhigh"])
        self.assertTrue(session["capabilities"]["setThinking"])
        self.assertEqual(session["messages"][0]["role"], "user")
        self.assertEqual(session["messages"][0]["content"], [{"type": "text", "text": "hello"}])
        self.assertIn("id", session["messages"][0])
        self.assertIn("createdAt", session["messages"][0])
        self.assertEqual(session["todos"], [{"content": "ship", "status": "pending"}])
        self.assertEqual(session["subagents"], [{"id": "agent-1", "status": "running"}])
        public = model.snapshot()
        self.assertEqual(set(public), {"generatedAt", "snapshotGeneration", "lastEventId", "sessions"})
        self.assertEqual(public["sessions"], [session])

    def test_rpc_thinking_uses_official_snake_case_and_missing_is_unknown(self):
        model = FleetModel()
        model.load_record(self.make_record())
        state = {
            "thinkingLevel": "max",
            "thinking_level": None,
            "model": {
                "provider": "openai",
                "id": "gpt-5",
                "reasoning": True,
                "thinking": {"mode": "effort", "efforts": ["low", "high"]},
            },
        }

        model.apply_rpc_snapshot(
            "deck-1",
            instance_id="instance-a",
            generation=3,
            snapshot=self.make_snapshot(**state),
        )

        session = model.get("deck-1")
        self.assertIsNone(session["thinking"])
        self.assertEqual(session["thinkingLevels"], ["off", "low", "high"])

    def test_lifecycle_uses_runtime_ownership_and_managed_intent(self):
        model = FleetModel()
        model.load_record(self.make_record(desiredRpcRunning=False))
        self.assertEqual(model.get("deck-1")["lifecycle"], "history")

        self.assertTrue(
            model.set_transport(
                "deck-1",
                instance_id="owned-runtime",
                generation=3,
                status="connected",
                reason=None,
            )
        )
        self.assertEqual(model.get("deck-1")["lifecycle"], "ongoing")

        self.assertTrue(
            model.set_transport(
                "deck-1",
                instance_id="owned-runtime",
                generation=3,
                status="disconnected",
                reason="collaboration connection lost",
            )
        )
        disconnected = model.get("deck-1")
        self.assertEqual(disconnected["lifecycle"], "ongoing")
        self.assertEqual(disconnected["transport"]["reason"], "collaboration connection lost")

    def test_desired_rpc_survives_disconnect_but_stop_becomes_history(self):
        model = FleetModel()
        model.load_record(self.make_record())
        self.assertEqual(model.get("deck-1")["lifecycle"], "ongoing")
        self.assertTrue(
            model.set_transport(
                "deck-1",
                instance_id=None,
                generation=3,
                status="disconnected",
                reason="machine unavailable",
            )
        )
        self.assertEqual(model.get("deck-1")["lifecycle"], "ongoing")

        model.load_record(self.make_record(generation=4, desiredRpcRunning=False))
        self.assertTrue(
            model.set_transport(
                "deck-1",
                instance_id=None,
                generation=4,
                status="disconnected",
                reason="shut down",
            )
        )
        stopped = model.get("deck-1")
        self.assertEqual(stopped["lifecycle"], "history")
        self.assertFalse(
            model.set_transport(
                "deck-1",
                instance_id="stale-runtime",
                generation=3,
                status="connected",
                reason=None,
            )
        )
        self.assertEqual(model.get("deck-1"), stopped)

    def test_restored_unowned_terminal_record_is_history(self):
        model = FleetModel()
        model.load_record(self.make_record(adapterKind="collab", desiredRpcRunning=False))
        session = model.get("deck-1")
        self.assertEqual(session["lifecycle"], "history")
        self.assertEqual(session["state"], "disconnected")

    def test_extension_message_end_only_marks_visible_assistant_content_unread(self):
        model = FleetModel()
        model.load_record(self.make_record(adapterKind="extension", desiredRpcRunning=False))
        self.assertTrue(
            model.set_transport(
                "deck-1",
                instance_id="extension-runtime",
                generation=3,
                status="connected",
                reason=None,
            )
        )
        self.assertTrue(
            model.apply_extension_event(
                "deck-1",
                instance_id="extension-runtime",
                generation=3,
                event={
                    "sequence": 1,
                    "kind": "message",
                    "data": {
                        "phase": "end",
                        "message": {"id": "tool-only", "role": "assistant", "content": []},
                    },
                },
            )
        )
        self.assertFalse(model.get("deck-1")["unread"])

        self.assertTrue(
            model.apply_extension_event(
                "deck-1",
                instance_id="extension-runtime",
                generation=3,
                event={
                    "sequence": 2,
                    "kind": "message",
                    "data": {
                        "phase": "end",
                        "message": {"id": "visible", "role": "assistant", "content": "done"},
                    },
                },
            )
        )
        self.assertTrue(model.get("deck-1")["unread"])

    def test_presentation_update_preserves_native_title_and_transport_ownership(self):
        model = self.connected_model()
        live_title = self.make_snapshot(sequence=1, session_name="Native live title")
        self.assertTrue(model.apply_rpc_snapshot("deck-1", instance_id="instance-a", generation=3, snapshot=live_title))

        session, snapshot_generation = model.update_presentation(
            self.make_record(
                displayName="My release",
                pinned=True,
                updatedAt="2026-09-03T13:00:00+00:00",
                presentationRevision=1,
            )
        )

        self.assertEqual(snapshot_generation, model.snapshot()["snapshotGeneration"])
        self.assertEqual(
            (session["title"], session["displayName"], session["pinned"]),
            ("Native live title", "My release", True),
        )
        self.assertEqual(session["transport"]["instanceId"], "instance-a")
        self.assertTrue(
            model.apply_rpc_snapshot(
                "deck-1",
                instance_id="instance-a",
                generation=3,
                snapshot=self.make_snapshot(sequence=2, session_name="New native title"),
            )
        )
        after_native_rename = model.get("deck-1")
        self.assertEqual(
            (after_native_rename["title"], after_native_rename["displayName"]),
            ("New native title", "My release"),
        )
        self.assertTrue(
            model.apply_adapter_event("deck-1", instance_id="instance-a", generation=3, event=self.event(3))
        )

        model.load_record(
            self.make_record(
                title="Stale persisted default",
                displayName="Stale Deck name",
                pinned=False,
                generation=4,
                ompSessionId="omp-from-record",
                updatedAt="2026-09-03T14:00:00+00:00",
            )
        )

        reloaded = model.get("deck-1")
        self.assertEqual(reloaded["title"], "New native title")
        self.assertEqual((reloaded["displayName"], reloaded["pinned"]), ("My release", True))
        self.assertEqual(reloaded["transport"]["instanceId"], "instance-a")
        self.assertTrue(
            model.apply_adapter_event("deck-1", instance_id="instance-a", generation=4, event=self.event(4))
        )

        newer, _ = model.update_presentation(
            self.make_record(
                displayName="Newest Deck name",
                pinned=False,
                presentationRevision=2,
                generation=4,
            )
        )
        stale, _ = model.update_presentation(
            self.make_record(
                displayName="Stale Deck name",
                pinned=True,
                presentationRevision=1,
                generation=3,
            )
        )
        self.assertEqual(
            (newer["displayName"], newer["pinned"]),
            ("Newest Deck name", False),
        )
        self.assertEqual(
            (stale["displayName"], stale["pinned"]),
            ("Newest Deck name", False),
        )

    def test_global_event_ids_are_monotonic_across_model_changes(self):
        model = self.connected_model()
        self.assertTrue(
            model.apply_adapter_event("deck-1", instance_id="instance-a", generation=3, event=self.event(1))
        )
        model.set_transport(
            "deck-1",
            instance_id="instance-a",
            generation=3,
            status="disconnected",
            reason="closed",
        )

        events = model.journal.replay_after(0)
        self.assertEqual([event.id for event in events], list(range(1, len(events) + 1)))
        self.assertEqual(model.snapshot()["lastEventId"], events[-1].id)
        self.assertEqual(
            [event.snapshot_generation for event in events],
            list(range(1, len(events) + 1)),
        )

    def test_rejects_duplicate_stale_instance_and_stale_generation_events(self):
        model = self.connected_model()
        accepted = self.event(1)
        self.assertTrue(model.apply_adapter_event("deck-1", instance_id="instance-a", generation=3, event=accepted))
        event_id = model.journal.latest_id

        self.assertFalse(model.apply_adapter_event("deck-1", instance_id="instance-a", generation=3, event=accepted))
        self.assertFalse(
            model.apply_adapter_event("deck-1", instance_id="instance-old", generation=3, event=self.event(2))
        )
        self.assertFalse(
            model.apply_adapter_event("deck-1", instance_id="instance-a", generation=2, event=self.event(2))
        )
        self.assertFalse(
            model.apply_rpc_snapshot(
                "deck-1", instance_id="instance-old", generation=3, snapshot=self.make_snapshot(sequence=2)
            )
        )
        self.assertFalse(
            model.set_transport(
                "deck-1",
                instance_id="instance-old",
                generation=3,
                status="disconnected",
                reason="stale",
            )
        )
        self.assertEqual(model.journal.latest_id, event_id)
        self.assertEqual(model.get("deck-1")["transport"]["instanceId"], "instance-a")

    def test_equal_generation_wrong_instance_cannot_replace_snapshot_or_transport(self):
        model = self.connected_model()
        before = model.get("deck-1")
        event_id = model.journal.latest_id

        self.assertFalse(
            model.apply_rpc_snapshot(
                "deck-1",
                instance_id="instance-b",
                generation=3,
                snapshot=self.make_snapshot(sequence=10, session_id="wrong-session"),
            )
        )
        self.assertFalse(
            model.set_transport(
                "deck-1",
                instance_id="instance-b",
                generation=3,
                status="disconnected",
                reason="wrong owner",
            )
        )

        self.assertEqual(model.get("deck-1"), before)
        self.assertEqual(model.journal.latest_id, event_id)

    def test_new_generation_resets_adapter_sequence_for_same_instance(self):
        model = self.connected_model()
        self.assertTrue(
            model.apply_adapter_event("deck-1", instance_id="instance-a", generation=3, event=self.event(1))
        )

        self.assertTrue(
            model.set_transport(
                "deck-1",
                instance_id="instance-a",
                generation=4,
                status="connected",
                reason=None,
            )
        )
        self.assertTrue(
            model.apply_adapter_event("deck-1", instance_id="instance-a", generation=4, event=self.event(1))
        )

    def test_sequence_gap_latches_until_authoritative_snapshot(self):
        model = self.connected_model()

        self.assertFalse(
            model.apply_adapter_event("deck-1", instance_id="instance-a", generation=3, event=self.event(2))
        )
        self.assertFalse(
            model.apply_adapter_event("deck-1", instance_id="instance-a", generation=3, event=self.event(1))
        )

        session = model.get("deck-1")
        self.assertEqual(session["transport"]["status"], "connected")
        self.assertEqual(session["transport"]["reason"], "adapter event gap")
        self.assertEqual(session["state"], "degraded")
        resync = model.journal.replay_after(0)[-1]
        self.assertEqual(resync.type, "session_resync_required")
        self.assertEqual(resync.payload["expectedSequence"], 1)
        self.assertEqual(resync.payload["receivedSequence"], 2)
        self.assertEqual(resync.payload["sessionPatch"]["state"], "degraded")
        self.assertTrue(
            model.apply_rpc_snapshot(
                "deck-1",
                instance_id="instance-a",
                generation=3,
                snapshot=self.make_snapshot(sequence=2),
            )
        )
        self.assertTrue(
            model.apply_adapter_event("deck-1", instance_id="instance-a", generation=3, event=self.event(3))
        )

    def test_sequence_gap_requires_snapshot_at_or_beyond_received_watermark(self):
        model = self.connected_model()
        self.assertFalse(
            model.apply_adapter_event("deck-1", instance_id="instance-a", generation=3, event=self.event(3))
        )

        self.assertFalse(
            model.apply_rpc_snapshot(
                "deck-1",
                instance_id="instance-a",
                generation=3,
                snapshot=self.make_snapshot(sequence=2),
            )
        )
        self.assertFalse(
            model.apply_adapter_event("deck-1", instance_id="instance-a", generation=3, event=self.event(3))
        )
        self.assertTrue(
            model.apply_rpc_snapshot(
                "deck-1",
                instance_id="instance-a",
                generation=3,
                snapshot=self.make_snapshot(sequence=3),
            )
        )
        self.assertTrue(
            model.apply_adapter_event("deck-1", instance_id="instance-a", generation=3, event=self.event(4))
        )

    def test_adapter_snapshot_collections_are_copied_into_fleet_state(self):
        model = FleetModel()
        model.load_record(self.make_record())
        messages = [{"role": "user", "content": [{"type": "text", "text": "original"}]}]
        todos = [{"content": "ship", "status": "pending", "meta": {"owner": "a"}}]
        subagents = [{"id": "agent-1", "status": "running", "meta": {"attempt": 1}}]
        snapshot = replace(
            self.make_snapshot(),
            messages=messages,
            todos=todos,
            subagents=subagents,
        )

        self.assertTrue(
            model.apply_rpc_snapshot(
                "deck-1",
                instance_id="instance-a",
                generation=3,
                snapshot=snapshot,
            )
        )
        messages[0]["content"][0]["text"] = "mutated"
        messages.append({"role": "assistant", "content": "late"})
        todos.append({"content": "late", "status": "pending"})
        subagents.append({"id": "agent-late", "status": "running"})
        todos[0]["meta"]["owner"] = "b"
        subagents[0]["meta"]["attempt"] = 2

        session = model.get("deck-1")
        self.assertEqual(session["messages"][0]["content"][0]["text"], "original")
        self.assertEqual(len(session["messages"]), 1)
        self.assertEqual(len(session["todos"]), 1)
        self.assertEqual(len(session["subagents"]), 1)
        self.assertEqual(session["todos"][0]["meta"]["owner"], "a")
        self.assertEqual(session["subagents"][0]["meta"]["attempt"], 1)

    def test_live_rpc_ui_request_uses_public_pending_request_shape(self):
        model = self.connected_model()
        payload = {
            "id": "ask-7",
            "kind": "select",
            "title": "Choose",
            "message": "Pick one",
            "options": [
                {"value": "safe", "label": "Safe", "description": "Recommended"},
            ],
            "allow_cancel": True,
            "allow_custom": False,
            "prefill": None,
            "custom_placeholder": None,
            "expires_at": None,
        }

        self.assertTrue(
            model.apply_adapter_event(
                "deck-1",
                instance_id="instance-a",
                generation=3,
                event=self.event(1, "ui_request", payload),
            )
        )

        pending = model.get("deck-1")["pendingUiRequest"]
        self.assertEqual(pending["id"], "ask-7")
        self.assertEqual(pending["questions"][0]["prompt"], "Pick one")
        self.assertEqual(
            pending["questions"][0]["options"],
            [{"value": "safe", "label": "Safe", "description": "Recommended"}],
        )
        self.assertTrue(pending["allowCancel"])
        self.assertNotIn("allow_cancel", pending)
        self.assertNotIn("allow_custom", pending["questions"][0])


class EventJournalTests(unittest.TestCase):
    @staticmethod
    def publish(journal, number, payload=None):
        return journal.publish(
            snapshot_generation=number,
            event_type="change",
            payload={"number": number} if payload is None else payload,
            session_id=f"session-{number % 3}",
            instance_id="instance-a",
            generation=1,
            sequence=number,
        )

    def test_replay_reports_a_gap_when_requested_id_predates_retained_window(self):
        journal = EventJournal(max_events=2, max_bytes=100_000)
        for number in range(1, 5):
            self.publish(journal, number)

        with self.assertRaises(ReplayUnavailableError):
            journal.replay_after(1)
        self.assertEqual([event.id for event in journal.replay_after(2)], [3, 4])

    def test_event_count_bound_evicts_oldest_events(self):
        journal = EventJournal(max_events=3, max_bytes=100_000)
        for number in range(1, 6):
            self.publish(journal, number)

        self.assertEqual([event.id for event in journal.replay_after(2)], [3, 4, 5])
        with self.assertRaises(ReplayUnavailableError):
            journal.replay_after(1)

    def test_fully_evicted_window_reports_replay_gap(self):
        journal = EventJournal(max_events=10, max_bytes=1)

        event = self.publish(journal, 1, {"text": "too large"})

        self.assertEqual(event.id, 1)
        self.assertEqual(journal.latest_id, 1)
        with self.assertRaises(ReplayUnavailableError):
            journal.replay_after(0)

    def test_future_cursor_is_unavailable_for_fresh_and_nonempty_journals(self):
        fresh = EventJournal()
        with self.assertRaises(ReplayUnavailableError):
            fresh.replay_after(1)

        populated = EventJournal()
        self.publish(populated, 1)
        with self.assertRaises(ReplayUnavailableError):
            populated.replay_after(2)

    def test_nested_payload_is_immutable_through_source_event_and_replay_objects(self):
        journal = EventJournal()
        source = {"nested": {"items": [{"value": "original"}]}}

        published = self.publish(journal, 1, source)
        source["nested"]["items"][0]["value"] = "source mutation"
        published.payload["nested"]["items"][0]["value"] = "event mutation"

        replayed = journal.replay_after(0)[0]
        self.assertEqual(replayed.payload["nested"]["items"][0]["value"], "original")
        replayed.payload["nested"]["items"][0]["value"] = "replay mutation"
        self.assertEqual(journal.replay_after(0)[0].payload["nested"]["items"][0]["value"], "original")

    def test_invalid_payloads_do_not_advance_latest_id(self):
        journal = EventJournal()
        cycle = {}
        cycle["self"] = cycle
        invalid_payloads = (
            {"value": float("nan")},
            {"value": float("inf")},
            {"value": float("-inf")},
            cycle,
            {"value": object()},
        )

        for payload in invalid_payloads:
            with self.subTest(payload=repr(payload)):
                before = journal.latest_id
                with self.assertRaises((TypeError, ValueError)):
                    self.publish(journal, 1, payload)
                self.assertEqual(journal.latest_id, before)

    def test_event_timestamp_is_rfc3339(self):
        event = self.publish(EventJournal(), 1)

        parsed = datetime.fromisoformat(event.timestamp)
        self.assertIsNotNone(parsed.tzinfo)
        self.assertIsNotNone(parsed.utcoffset())
        self.assertRegex(
            event.timestamp,
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$",
        )

    def test_concurrent_publishers_receive_one_contiguous_global_order(self):
        publisher_count = 8
        events_per_publisher = 40
        journal = EventJournal(max_events=publisher_count * events_per_publisher, max_bytes=1_000_000)
        barrier = threading.Barrier(publisher_count)

        def publish_batch(publisher):
            barrier.wait()
            return [
                self.publish(journal, publisher * events_per_publisher + offset)
                for offset in range(events_per_publisher)
            ]

        with ThreadPoolExecutor(max_workers=publisher_count) as executor:
            batches = list(executor.map(publish_batch, range(publisher_count)))

        returned = [event for batch in batches for event in batch]
        replayed = journal.replay_after(0)
        expected_count = publisher_count * events_per_publisher
        self.assertEqual(sorted(event.id for event in returned), list(range(1, expected_count + 1)))
        self.assertEqual([event.id for event in replayed], list(range(1, expected_count + 1)))
        self.assertEqual(
            {event.payload["number"] for event in replayed},
            set(range(expected_count)),
        )


if __name__ == "__main__":
    unittest.main()
