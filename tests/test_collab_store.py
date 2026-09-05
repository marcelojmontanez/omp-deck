import json
import multiprocessing
import os
import stat
import tempfile
import threading
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from omp_deck.collab_protocol import b64url_encode
from omp_deck.collab_store import (
    MAX_FILE_BYTES,
    CollabStore,
    CollabStoreCorruptionError,
    CollabStoreValidationError,
)

ROOM = "room_identifier_123"
KEY = b"k" * 32
TOKEN = b"t" * 16
WRITE_LINK = f"wss://relay.example/r/{ROOM}#{b64url_encode(KEY + TOKEN)}"
VIEW_LINK = f"wss://relay.example/r/{ROOM}#{b64url_encode(KEY)}"


def _save_in_process(root, index, start, failures):
    try:
        start.wait()
        CollabStore(root).put(f"process-{index}", WRITE_LINK)
    except BaseException as exc:
        failures.put(f"{type(exc).__name__}: {exc}")


class CollabStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "state"
        self.store = CollabStore(
            self.root,
            clock=lambda: datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_round_trip_and_public_projection_redact_secret(self):
        view = self.store.put("deck-session", WRITE_LINK)
        self.assertEqual(self.store.get_link("deck-session"), WRITE_LINK)
        self.assertEqual(self.store.get("deck-session"), view)
        self.assertEqual(self.store.list(), [view])
        public_json = json.dumps({"session": view, "sessions": self.store.list()})
        for forbidden in (WRITE_LINK, b64url_encode(KEY), b64url_encode(TOKEN), "link", "key", "token", "secret"):
            self.assertNotIn(forbidden, public_json.lower())
        self.assertEqual(set(view), {"sessionId", "createdAt", "updatedAt"})
        self.assertNotIn("room", public_json)
        self.assertNotIn("relay", public_json)

    def test_view_link_is_rejected_without_leaking_it(self):
        with self.assertRaises(CollabStoreValidationError) as raised:
            self.store.put("deck-session", VIEW_LINK)
        self.assertNotIn(VIEW_LINK, str(raised.exception))
        self.assertNotIn(b64url_encode(KEY), repr(raised.exception))

    def test_file_and_lock_are_owner_only(self):
        self.store.put("deck-session", WRITE_LINK)
        self.assertEqual(stat.S_IMODE(self.store.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.root / ".collab.json.lock").stat().st_mode), 0o600)
        os.chmod(self.store.path, 0o644)
        CollabStore(self.root).list()
        self.assertEqual(stat.S_IMODE(self.store.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)

    def test_corruption_is_quarantined_without_secret_in_error(self):
        self.root.mkdir()
        leaked = "super-secret-token"
        self.store.path.write_text('{"link":"' + leaked, encoding="utf-8")
        with self.assertRaises(CollabStoreCorruptionError) as raised:
            self.store.list()
        error = raised.exception
        self.assertNotIn(leaked, str(error))
        self.assertNotIn(leaked, repr(error))
        self.assertFalse(self.store.path.exists())
        self.assertTrue(error.quarantine_path.exists())
        self.assertEqual(stat.S_IMODE(error.quarantine_path.stat().st_mode), 0o600)

    def test_concurrent_instances_do_not_lose_records(self):
        stores = [CollabStore(self.root) for _ in range(20)]
        barrier = threading.Barrier(len(stores))
        failures = []

        def save(index):
            try:
                barrier.wait()
                stores[index].put(f"session-{index}", WRITE_LINK)
            except BaseException as exc:
                failures.append(exc)

        threads = [threading.Thread(target=save, args=(i,)) for i in range(len(stores))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(failures, [])
        self.assertEqual({item["sessionId"] for item in self.store.list()}, {f"session-{i}" for i in range(20)})

    def test_concurrent_processes_do_not_lose_records(self):
        context = multiprocessing.get_context("fork")
        start = context.Event()
        failures = context.Queue()
        processes = [
            context.Process(target=_save_in_process, args=(self.root, index, start, failures)) for index in range(12)
        ]
        for process in processes:
            process.start()
        start.set()

        for process in processes:
            process.join(10)
            self.assertEqual(process.exitcode, 0)
        self.assertTrue(failures.empty())
        self.assertEqual(
            {item["sessionId"] for item in self.store.list()},
            {f"process-{index}" for index in range(12)},
        )

    def test_chmod_error_is_not_quarantined(self):
        self.store.put("session", WRITE_LINK)
        with mock.patch("omp_deck.collab_store.os.chmod", side_effect=PermissionError("denied")):
            with self.assertRaisesRegex(PermissionError, "denied"):
                self.store.list()
        self.assertTrue(self.store.path.exists())
        self.assertEqual(list(self.root.glob("collab.json.corrupt.*")), [])

    def test_operational_read_error_is_not_quarantined(self):
        self.store.put("session", WRITE_LINK)
        with mock.patch("omp_deck.collab_store.os.read", side_effect=OSError("device failure")):
            with self.assertRaisesRegex(OSError, "device failure"):
                self.store.list()
        self.assertTrue(self.store.path.exists())
        self.assertEqual(list(self.root.glob("collab.json.corrupt.*")), [])

    def test_oversized_and_deep_documents_are_quarantined(self):
        cases = (
            b" " * (MAX_FILE_BYTES + 1),
            b'{"version":1,"records":' + b"[" * 65 + b"]" * 65 + b"}",
        )
        for index, content in enumerate(cases):
            with self.subTest(index=index):
                self.root.mkdir(mode=0o700, exist_ok=True)
                self.store.path.write_bytes(content)
                with self.assertRaises(CollabStoreCorruptionError):
                    self.store.list()

    def test_surrogates_and_unicode_controls_are_rejected(self):
        for unsafe in ("\ud800", "\u202e", "\u2066", "\x1f"):
            with self.subTest(character=ascii(unsafe)):
                link = WRITE_LINK.replace(ROOM, ROOM + unsafe)
                with self.assertRaises(CollabStoreValidationError):
                    self.store.put("session", link)
        self.root.mkdir(mode=0o700, exist_ok=True)
        document = {
            "version": 1,
            "records": [
                {
                    "sessionId": "session",
                    "link": WRITE_LINK,
                    "relay": "wss://relay.example/\ud800",
                    "room": ROOM,
                    "createdAt": "2026-01-02T03:04:05Z",
                    "updatedAt": "2026-01-02T03:04:05Z",
                }
            ],
        }
        self.store.path.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(CollabStoreCorruptionError):
            self.store.list()

    def test_symlink_roots_lock_and_data_paths_are_rejected(self):
        target = Path(self.temp.name) / "target"
        target.mkdir()
        root_link = Path(self.temp.name) / "root-link"
        root_link.symlink_to(target, target_is_directory=True)
        with self.assertRaises(OSError):
            CollabStore(root_link).list()

        self.root.mkdir(mode=0o700)
        outside = Path(self.temp.name) / "outside"
        outside.write_text("untouched", encoding="utf-8")

        regular_file = Path(self.temp.name) / "not-a-directory"
        regular_file.write_text("untouched", encoding="utf-8")
        with self.assertRaises(OSError):
            CollabStore(regular_file).list()
        with mock.patch("omp_deck.collab_store.os.geteuid", return_value=os.geteuid() + 1):
            with self.assertRaises(PermissionError):
                CollabStore(target).list()
        self.store.path.symlink_to(outside)
        with self.assertRaises(OSError):
            self.store.list()
        self.store.path.unlink()
        (self.root / ".collab.json.lock").unlink()
        (self.root / ".collab.json.lock").symlink_to(outside)
        with self.assertRaises(OSError):
            self.store.list()
        self.assertEqual(outside.read_text(encoding="utf-8"), "untouched")

    def test_serialization_error_does_not_chain_secret_document(self):
        self.store.put("session", WRITE_LINK)
        data = self.store._read_or_quarantine()
        secret = repr(data)
        with mock.patch(
            "omp_deck.collab_store.json.dumps",
            side_effect=ValueError(secret),
        ):
            with self.assertRaises(CollabStoreValidationError) as raised:
                self.store._write(data)
        self.assertNotIn(WRITE_LINK, repr(raised.exception))
        self.assertTrue(raised.exception.__suppress_context__)

    def test_remove_atomically_rewrites_without_removed_credential(self):
        second = WRITE_LINK.replace(ROOM, "another_room_456")
        self.store.put("one", WRITE_LINK)
        self.store.put("two", second)
        self.assertTrue(self.store.remove("one"))
        self.assertIsNone(self.store.get_link("one"))
        self.assertEqual(self.store.get_link("two"), second)
        self.assertNotIn(WRITE_LINK, self.store.path.read_text(encoding="utf-8"))
        self.assertFalse(self.store.remove("missing"))


if __name__ == "__main__":
    unittest.main()
