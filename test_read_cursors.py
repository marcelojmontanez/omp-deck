import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from omp_deck.fleet_model import FleetModel
from omp_deck.read_cursors import CursorStore
from omp_deck.rpc_adapter import AdapterSnapshot


class CursorStoreTests(unittest.TestCase):
    def test_read_cursor_survives_model_restart_and_new_message_becomes_unread(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cursors.json"
            record = {
                "id": "deck-1",
                "title": "Deck",
                "project": {"name": "deck", "path": "/work/deck"},
                "adapterKind": "rpc",
                "ompSessionId": None,
                "generation": 1,
                "updatedAt": "2026-09-03T12:00:00+00:00",
            }
            snapshot = AdapterSnapshot(
                state={"session_id": "omp-1", "is_streaming": False},
                messages=({"id": "assistant-1", "role": "assistant", "content": "done"},),
                todos=(),
                subagents=(),
                sequence=0,
                pending_ui_request=None,
                transport_status="connected",
                reason=None,
            )

            model = FleetModel(cursor_store=CursorStore(path))
            model.load_record(record)
            model.apply_rpc_snapshot("deck-1", instance_id="one", generation=1, snapshot=snapshot)
            self.assertTrue(model.get("deck-1")["unread"])
            self.assertFalse(model.mark_read("deck-1")["unread"])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

            restarted = FleetModel(cursor_store=CursorStore(path))
            restarted.load_record(record)
            restarted.apply_rpc_snapshot("deck-1", instance_id="two", generation=1, snapshot=snapshot)
            self.assertFalse(restarted.get("deck-1")["unread"])

            newer = replace(
                snapshot,
                messages=(*snapshot.messages, {"id": "assistant-2", "role": "assistant", "content": "new"}),
                sequence=1,
            )
            restarted.apply_rpc_snapshot("deck-1", instance_id="two", generation=1, snapshot=newer)
            self.assertTrue(restarted.get("deck-1")["unread"])

    def test_corrupt_store_is_quarantined_and_reported_unhealthy(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cursors.json"
            path.write_text("not-json", encoding="utf-8")

            store = CursorStore(path)

            self.assertFalse(store.healthy)
            self.assertIsNotNone(store.quarantined_path)
            self.assertFalse(path.exists())
            self.assertEqual(store.last_read("deck-1"), None)
            self.assertEqual(store.quarantined_path.read_text(encoding="utf-8"), "not-json")

    def test_symlink_store_is_never_read(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            target = Path(outside) / "private.json"
            target.write_text(json.dumps({"version": 1, "sessions": {"secret": {"messageId": "x"}}}))
            path = Path(directory) / "cursors.json"
            path.symlink_to(target)

            store = CursorStore(path)

            self.assertFalse(store.healthy)
            self.assertEqual(store.last_read("secret"), None)
            self.assertEqual(target.read_text(), json.dumps({"version": 1, "sessions": {"secret": {"messageId": "x"}}}))


if __name__ == "__main__":
    unittest.main()
