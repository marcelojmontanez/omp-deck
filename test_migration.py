import json
import tempfile
import unittest
from pathlib import Path

from omp_deck._terminal_discovery import terminal_id
from omp_deck.migration import apply_migration, migration_plan
from omp_deck.read_cursors import CursorStore
from omp_deck.registry_store import RegistryStore
from omp_deck.settings import Settings


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.projects = self.root / "projects"
        self.project = self.projects / "deck"
        self.project.mkdir(parents=True)
        self.state = self.root / "state"
        self.sockets = self.state / "sockets"
        self.sockets.mkdir(parents=True, mode=0o700)
        self.sockets.chmod(0o700)
        self.state.chmod(0o700)
        self.session_id = "01a0664c-4f44-768e-aef0-3adde4f0395a"
        self.settings = Settings(
            host="127.0.0.1",
            port=8877,
            session_root=self.root / "omp-sessions",
            socket_root=self.sockets,
            state_root=self.state,
            project_root=self.projects,
            omp_bin="omp",
            allowed_hosts=("127.0.0.1",),
        )

    def write_legacy_state(self):
        metadata = self.sockets / f"{self.session_id}.123.connection.sock.json"
        metadata.write_text(json.dumps({
            "sessionId": self.session_id,
            "cwd": str(self.project),
            "title": "Existing terminal session",
        }))
        metadata.chmod(0o600)
        cursors = self.state / "read-cursors.json"
        cursors.write_text(json.dumps({
            "version": 1,
            "sessions": {self.session_id: {"eventId": "legacy-event-7"}},
        }))
        cursors.chmod(0o664)

    def test_plan_is_read_only_and_apply_is_idempotent(self):
        self.write_legacy_state()

        plan = migration_plan(self.settings)

        self.assertEqual(plan["legacySessions"], 1)
        self.assertEqual(plan["legacyReadCursors"], 1)
        self.assertFalse((self.state / "registry.json").exists())
        self.assertFalse(self.settings.cursor_path.exists())

        result = apply_migration(self.settings)
        session_id = terminal_id(self.session_id)
        record = RegistryStore(self.state / "registry.json").load()[0]
        self.assertEqual(result["sessionsImported"], 1)
        self.assertEqual(result["readCursorsImported"], 1)
        self.assertEqual(record["id"], session_id)
        self.assertEqual(record["adapterKind"], "extension")
        self.assertEqual(record["project"], {"name": "deck", "path": str(self.project)})
        self.assertEqual(CursorStore(self.settings.cursor_path).last_read(session_id), "legacy-event-7")
        self.assertEqual(self.settings.cursor_path.stat().st_mode & 0o777, 0o600)

        repeated = apply_migration(self.settings)
        self.assertEqual(
            (self.state / ".omp-deck-owned").read_text(encoding="utf-8"),
            "OMP Deck owned directory\n",
        )
        self.assertEqual(repeated["sessionsImported"], 0)
        self.assertEqual(len(RegistryStore(self.state / "registry.json").load()), 1)

    def test_unsafe_metadata_and_project_escape_are_ignored(self):
        outside = self.root / "outside"
        outside.mkdir()
        metadata = self.sockets / f"{self.session_id}.123.connection.sock.json"
        metadata.write_text(json.dumps({"sessionId": self.session_id, "cwd": str(outside)}))
        metadata.chmod(0o666)

        self.assertEqual(migration_plan(self.settings)["legacySessions"], 0)
        self.assertEqual(apply_migration(self.settings)["sessionsImported"], 0)


    def test_apply_refuses_to_claim_unknown_legacy_state(self):
        (self.state / "unrelated-private-data").write_text("do not delete")

        with self.assertRaisesRegex(RuntimeError, "unknown entries"):
            apply_migration(self.settings)

        self.assertFalse((self.state / ".omp-deck-owned").exists())

    def test_apply_rejects_permissive_state_root_without_claiming_it(self):
        self.state.chmod(0o777)

        with self.assertRaisesRegex(RuntimeError, "unsafe ownership or permissions"):
            apply_migration(self.settings)

        self.assertEqual(self.state.stat().st_mode & 0o777, 0o777)
        self.assertFalse((self.state / ".omp-deck-owned").exists())
        self.assertFalse((self.state / "registry.json").exists())

if __name__ == "__main__":
    unittest.main()
