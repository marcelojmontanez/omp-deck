import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from omp_deck.registry_store import (
    RegistryCapacityError,
    RegistryCorruptionError,
    RegistryError,
    RegistryStore,
    RegistryValidationError,
)

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


class RegistryStoreTests(unittest.TestCase):
    def setUp(self):
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary_directory.cleanup)
        self.tmp_path = Path(self._temporary_directory.name)

    def make_store(self, **overrides):
        ids = iter(f"deck-{number}" for number in range(100))
        options = {"clock": lambda: NOW, "id_factory": lambda: next(ids)}
        options.update(overrides)
        return RegistryStore(self.tmp_path / "registry.json", **options)

    def create_session(self, store, **overrides):
        values = {
            "adapter_kind": "rpc",
            "omp_session_id": "omp-7",
            "project_name": "deck",
            "project_path": "/work/deck",
            "title": "Build Deck",
            "desired_rpc_running": True,
            "recovery_metadata": {"argv": ["omp", "--mode", "rpc"], "attempt": 2},
        }
        values.update(overrides)
        return store.create(**values)

    def test_restart_preserves_stable_identity_and_updates_generation(self):
        store = self.make_store()
        created = self.create_session(store)

        restarted = RegistryStore(self.tmp_path / "registry.json")
        self.assertEqual(restarted.load(), [created])

        updated = restarted.update(created["id"], title="Resumed", omp_session_id="omp-8", desired_rpc_running=False)
        self.assertEqual(updated["id"], created["id"])
        self.assertGreater(updated["generation"], created["generation"])
        self.assertEqual(RegistryStore(self.tmp_path / "registry.json").load(), [updated])
        self.assertTrue(restarted.remove(created["id"]))
        self.assertFalse(restarted.remove(created["id"]))
        self.assertEqual(RegistryStore(self.tmp_path / "registry.json").load(), [])

    def test_presentation_metadata_persists_without_rotating_transport_generation(self):
        store = self.make_store()
        created = self.create_session(store)

        renamed = store.update_presentation(created["id"], display_name="Release work", pinned=True)

        self.assertEqual(renamed["generation"], created["generation"])
        self.assertEqual((renamed["displayName"], renamed["pinned"]), ("Release work", True))
        self.assertEqual(renamed["presentationRevision"], 1)
        self.assertEqual(renamed["updatedAt"], created["updatedAt"])
        self.assertEqual(
            RegistryStore(self.tmp_path / "registry.json").load()[0],
            renamed,
        )

        reset = store.update_presentation(created["id"], display_name=None, pinned=False)
        self.assertEqual(reset["generation"], created["generation"])
        self.assertEqual((reset["displayName"], reset["pinned"]), (None, False))
        self.assertEqual(reset["presentationRevision"], 2)

    def test_concurrent_presentation_updates_receive_ordered_durable_revisions(self):
        store = self.make_store()
        created = self.create_session(store)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(
                    lambda name: RegistryStore(self.tmp_path / "registry.json").update_presentation(
                        created["id"],
                        display_name=name,
                    ),
                    ("First", "Second"),
                )
            )

        self.assertEqual({record["presentationRevision"] for record in results}, {1, 2})
        durable = RegistryStore(self.tmp_path / "registry.json").load()[0]
        latest = max(results, key=lambda record: record["presentationRevision"])
        self.assertEqual(
            (durable["presentationRevision"], durable["displayName"]),
            (latest["presentationRevision"], latest["displayName"]),
        )

    def test_version_one_registry_is_upgraded_without_losing_sessions(self):
        store = self.make_store()
        created = self.create_session(store)
        legacy = json.loads(store.path.read_text(encoding="utf-8"))
        legacy["version"] = 1
        for record in legacy["sessions"]:
            record.pop("displayName")
            record.pop("pinned")
            record.pop("presentationRevision")
        store.path.write_text(json.dumps(legacy), encoding="utf-8")

        loaded = RegistryStore(self.tmp_path / "registry.json").load()

        self.assertEqual(loaded[0]["id"], created["id"])
        self.assertEqual(
            (loaded[0]["displayName"], loaded[0]["pinned"], loaded[0]["presentationRevision"]),
            (None, False, 0),
        )
        self.assertEqual(json.loads(store.path.read_text(encoding="utf-8"))["version"], 2)

    def test_registry_and_lock_are_owner_only(self):
        store = self.make_store()
        self.create_session(store)
        if os.name == "nt":
            from omp_deck._windows_security import is_private_path

            store.load()
            self.assertTrue(is_private_path(store.path))
            self.assertTrue(is_private_path(store._lock_path))
        else:
            os.chmod(store.path, 0o644)
            store.load()
            self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(store._lock_path.stat().st_mode & 0o777, 0o600)

    @unittest.skipIf(os.name == "nt", "Windows symlink creation requires elevated privileges")
    def test_symlinked_lock_is_rejected_without_changing_target(self):
        victim = self.tmp_path / "victim"
        victim.write_text("unrelated", encoding="utf-8")
        victim.chmod(0o644)
        store = self.make_store()
        store._lock_path.symlink_to(victim)

        with self.assertRaises(RegistryError):
            store.load()

        self.assertTrue(store._lock_path.is_symlink())
        self.assertEqual(victim.stat().st_mode & 0o777, 0o644)
        self.assertEqual(victim.read_text(encoding="utf-8"), "unrelated")

    def test_invalid_persisted_schema_is_quarantined_with_explicit_error(self):
        path = self.tmp_path / "registry.json"
        path.write_text(json.dumps({"version": 99, "generation": 0, "sessions": []}))
        store = RegistryStore(path, clock=lambda: NOW)

        with self.assertRaises(RegistryCorruptionError) as raised:
            store.load()

        self.assertFalse(path.exists())
        self.assertTrue(raised.exception.quarantine_path.exists())
        self.assertTrue(raised.exception.quarantine_path.read_text().startswith('{"version": 99'))

    def test_malformed_json_is_quarantined_and_not_silently_reset(self):
        path = self.tmp_path / "registry.json"
        path.write_text("not-json")

        with self.assertRaises(RegistryCorruptionError):
            RegistryStore(path, clock=lambda: NOW).load()

        self.assertFalse(path.exists())
        self.assertTrue(list(self.tmp_path.glob("registry.json.corrupt.*")))

    def test_record_bound_is_enforced_without_changing_disk(self):
        store = self.make_store(max_records=1)
        first = self.create_session(store)

        with self.assertRaises(RegistryCapacityError):
            self.create_session(store, title="Too many")

        self.assertEqual(store.load(), [first])

    def test_secret_and_transcript_fields_are_rejected(self):
        metadata_values = [
            {"collabWriteLink": "https://example.invalid/write"},
            {"nested": {"transcript": "private conversation"}},
            {"credentials": {"token": "private"}},
            {"messages": []},
            {"content": "conversation text"},
        ]
        for metadata in metadata_values:
            with self.subTest(metadata=metadata):
                store = self.make_store()
                with self.assertRaises(RegistryValidationError):
                    self.create_session(store, recovery_metadata=metadata)
                self.assertEqual(store.load(), [])

    def test_invalid_requested_schema_is_rejected_without_corrupting_registry(self):
        store = self.make_store()
        first = self.create_session(store)

        with self.assertRaises(RegistryValidationError):
            store.update(first["id"], adapter_kind="terminal")

        self.assertEqual(RegistryStore(self.tmp_path / "registry.json").load(), [first])

    def test_duplicate_injected_ids_are_rejected(self):
        store = self.make_store()
        self.create_session(store, session_id="stable")

        with self.assertRaisesRegex(RegistryValidationError, "duplicate"):
            self.create_session(store, session_id="stable")

    def test_concurrent_store_instances_do_not_lose_updates(self):
        initial = self.make_store()
        records = [self.create_session(initial, session_id=f"stable-{index}") for index in range(20)]

        def update(index):
            independent_store = RegistryStore(self.tmp_path / "registry.json", clock=lambda: NOW)
            return independent_store.update(f"stable-{index}", title=f"updated-{index}")

        with ThreadPoolExecutor(max_workers=8) as executor:
            updated = list(executor.map(update, range(20)))

        loaded = RegistryStore(self.tmp_path / "registry.json").load()
        self.assertEqual(
            {record["title"] for record in loaded},
            {f"updated-{index}" for index in range(20)},
        )
        self.assertEqual(len({record["generation"] for record in updated}), 20)
        self.assertGreater(
            min(record["generation"] for record in updated),
            max(record["generation"] for record in records),
        )
