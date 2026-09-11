import unittest
from unittest.mock import patch

from omp_deck.fleet_model import FleetModel
from omp_deck.rpc_adapter import AdapterEvent, AdapterSnapshot


class FleetActivityTests(unittest.TestCase):
    def setUp(self):
        self.model = FleetModel()
        self.sequence = 0
        self.model.load_record(
            {
                "id": "deck",
                "title": "Original title",
                "project": {"name": "project", "path": "/work/project"},
                "adapterKind": "rpc",
                "generation": 1,
                "updatedAt": "2026-08-01T00:00:00+00:00",
            }
        )

    def snapshot(self, *, messages=(), todos=(), **state):
        self.model.apply_rpc_snapshot(
            "deck",
            instance_id="rpc",
            generation=1,
            snapshot=AdapterSnapshot(
                state=state,
                messages=messages,
                todos=todos,
                subagents=(),
                sequence=self.sequence,
                pending_ui_request=None,
                transport_status="connected",
                reason=None,
            ),
        )
        return self.model.get("deck")

    def event(self, kind, **payload):
        self.sequence += 1
        self.model.apply_adapter_event(
            "deck",
            instance_id="rpc",
            generation=1,
            event=AdapterEvent(self.sequence, kind, payload, float(self.sequence)),
        )
        return self.model.get("deck")

    def test_native_todo_phases_expose_real_tasks_and_session_renames(self):
        session = self.snapshot(
            session_name="Renamed in OMP",
            todos=(
                {"id": "build", "name": "Build", "tasks": [{"id": "ship", "content": "Ship", "status": "in_progress"}]},
            ),
        )
        self.assertEqual(session["title"], "Renamed in OMP")
        self.assertEqual(
            session["todos"], [{"id": "ship", "content": "Ship", "status": "in_progress", "phase": "Build"}]
        )

    def test_identical_refresh_preserves_activity_age_until_state_changes(self):
        with patch("omp_deck.fleet_model.now", return_value="2026-09-01T00:00:00+00:00"):
            first = self.snapshot(is_streaming=False)
        with patch("omp_deck.fleet_model.now", return_value="2026-09-02T00:00:00+00:00"):
            same = self.snapshot(is_streaming=False)
            changed = self.snapshot(is_streaming=True)
        self.assertEqual(same["updatedAt"], first["updatedAt"])
        self.assertEqual(changed["updatedAt"], "2026-09-02T00:00:00+00:00")

    def test_overlapping_tools_and_intermediate_turns_do_not_finish_the_agent(self):
        self.snapshot(is_streaming=True)
        self.event("tool_execution_start", tool_call_id="a", tool_name="read", intent="Reading project")
        self.event("tool_execution_start", tool_call_id="b", tool_name="bash", intent="Checking build")
        turn = self.event("turn_end")
        self.assertEqual((turn["state"], turn["workingOn"]), ("working", "Checking build"))
        remaining = self.event("tool_execution_end", tool_call_id="b", tool_name="bash")
        self.assertEqual(remaining["workingOn"], "Reading project")
        continuing = self.event("agent_end", is_terminal=False, messages=[])
        self.assertEqual(continuing["state"], "working")
        finished = self.event("agent_end", is_terminal=True, messages=[])
        self.assertEqual((finished["state"], finished["workingOn"]), ("idle", None))

    def test_authoritative_idle_snapshot_clears_unfinished_tool_activity(self):
        self.snapshot(is_streaming=True)
        active = self.event("tool_execution_start", tool_call_id="a", tool_name="read", intent="Old task")
        self.assertEqual(active["workingOn"], "Old task")
        session = self.snapshot(is_streaming=False)
        self.assertEqual((session["state"], session["workingOn"]), ("idle", None))

    def test_failed_assistant_completion_exposes_error_instead_of_empty_idle_reply(self):
        session = self.snapshot(
            is_streaming=False,
            messages=(
                {"role": "assistant", "content": [], "stopReason": "error", "errorMessage": "Provider unavailable"},
            ),
        )
        self.assertEqual(session["state"], "failed")
        self.assertEqual(session["messages"][0]["content"], [{"type": "text", "text": "Provider unavailable"}])
