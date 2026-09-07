import json
import multiprocessing
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from omp_deck.fleet_model import FleetModel
from omp_deck.read_cursors import CursorStore
from omp_deck.rpc_adapter import AdapterSnapshot


def _mark_cursor_in_process(path, index, start, failures):
    try:
        start.wait()
        CursorStore(path).mark_read(f"session-{index}", f"message-{index}")
        failures.put(None)
    except BaseException as error:
        failures.put(f"{type(error).__name__}: {error}")


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
            if os.name == "nt":
                from omp_deck._windows_security import is_private_path

                self.assertTrue(is_private_path(path))
            else:
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

    def test_concurrent_processes_do_not_lose_cursor_updates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cursors.json"
            context = multiprocessing.get_context("spawn" if os.name == "nt" else "fork")
            start = context.Event()
            failures = context.Queue()
            processes = [
                context.Process(
                    target=_mark_cursor_in_process,
                    args=(path, index, start, failures),
                )
                for index in range(12)
            ]
            for process in processes:
                process.start()
            start.set()
            for process in processes:
                process.join(10)
                self.assertEqual(process.exitcode, 0)

            outcomes = [failures.get(timeout=2) for _ in processes]
            self.assertEqual(outcomes, [None] * len(processes))
            store = CursorStore(path)
            self.assertEqual(
                {store.last_read(f"session-{index}") for index in range(12)},
                {f"message-{index}" for index in range(12)},
            )

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

    @unittest.skipIf(os.name == "nt", "Windows symlink creation requires elevated privileges")
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
            self.assertTrue(path.is_symlink())
            self.assertIsNone(store.quarantined_path)

    @unittest.skipIf(os.name == "nt", "Windows symlink creation requires elevated privileges")
    def test_importing_server_does_not_touch_cursor_state_before_authentication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_root = root / "config"
            state_root = root / "state"
            config_root.mkdir(mode=0o700)
            state_root.mkdir(mode=0o700)
            config = config_root / "config.json"
            config.write_text(json.dumps({"state_root": str(state_root)}), encoding="utf-8")
            config.chmod(0o600)
            victim = root / "victim"
            victim.write_text("private", encoding="utf-8")
            cursor = state_root / "cursors.json"
            cursor.symlink_to(victim)
            environment = {
                **os.environ,
                "OMP_DECK_CONFIG": str(config),
                "PYTHONPATH": str(Path(__file__).resolve().parent.parent / "src"),
            }

            result = subprocess.run(
                [sys.executable, "-c", "import omp_deck.server"],
                env=environment,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(cursor.is_symlink())
            self.assertEqual(victim.read_text(encoding="utf-8"), "private")


if __name__ == "__main__":
    unittest.main()
