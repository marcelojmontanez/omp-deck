import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from omp_deck.request_ledger import RequestConflictError, RequestLedger


class RequestLedgerTests(unittest.TestCase):
    def test_pending_and_completed_requests_survive_new_store_instances(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "requests.json"
            first = RequestLedger(path)
            self.assertIsNone(first.reserve("rpc-command:session", "request-1", "a" * 64))

            pending = RequestLedger(path).reserve("rpc-command:session", "request-1", "a" * 64)
            self.assertEqual(
                pending,
                {
                    "requestId": "request-1",
                    "status": "uncertain",
                    "errorCode": "outcome_unknown",
                },
            )

            accepted = {
                "requestId": "request-1",
                "status": "accepted",
                "result": None,
            }
            first.complete("rpc-command:session", "request-1", "a" * 64, accepted)
            self.assertEqual(
                RequestLedger(path).reserve("rpc-command:session", "request-1", "a" * 64),
                accepted,
            )
            with self.assertRaises(RequestConflictError):
                RequestLedger(path).reserve("rpc-command:session", "request-1", "b" * 64)

            if os.name == "nt":
                from omp_deck._windows_security import is_private_path

                self.assertTrue(is_private_path(path))
                self.assertTrue(is_private_path(first.lock_path))
            else:
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(first.lock_path.stat().st_mode & 0o777, 0o600)

    def test_accepted_message_receipt_survives_restart_until_explicit_dismissal(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "requests.json"
            ledger = RequestLedger(path, clock=lambda: 100.0)
            recovery = {
                "kind": "message",
                "sessionId": "session",
                "action": "sendMessage",
                "message": "same text",
                "images": [],
            }
            self.assertIsNone(
                ledger.reserve(
                    "rpc-command:session",
                    "message-1",
                    "a" * 64,
                    recovery=recovery,
                )
            )
            ledger.complete(
                "rpc-command:session",
                "message-1",
                "a" * 64,
                {
                    "requestId": "message-1",
                    "status": "accepted",
                    "acceptance": "accepted",
                    "result": None,
                },
            )

            restarted = RequestLedger(path, clock=lambda: 101.0)
            accepted = restarted.deliveries("session")
            self.assertEqual(
                (accepted[0]["requestId"], accepted[0]["state"], accepted[0]["message"]),
                ("message-1", "accepted", "same text"),
            )
            self.assertEqual(accepted[0]["expiresAt"], 100_000 + 7 * 24 * 60 * 60 * 1_000)

            self.assertTrue(restarted.dismiss_delivery("session", "message-1"))
            self.assertEqual(restarted.deliveries("session"), [])
            self.assertFalse(restarted.dismiss_delivery("session", "message-1"))
            self.assertEqual(
                restarted.lookup("rpc-command:session", "message-1", "a" * 64),
                {
                    "requestId": "message-1",
                    "status": "accepted",
                    "acceptance": "accepted",
                    "result": None,
                },
            )

    def test_uncertain_identical_messages_remain_distinct_without_text_or_order_inference(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = RequestLedger(Path(directory) / "requests.json")
            for index in (1, 2):
                request_id = f"message-{index}"
                fingerprint = str(index) * 64
                ledger.reserve(
                    "rpc-command:session",
                    request_id,
                    fingerprint,
                    recovery={
                        "kind": "message",
                        "sessionId": "session",
                        "action": "sendMessage",
                        "message": "identical",
                        "images": [],
                    },
                )
                ledger.mark_uncertain("rpc-command:session", request_id, fingerprint)

            restarted = RequestLedger(ledger.path)
            self.assertEqual(
                [(item["requestId"], item["state"], item["message"]) for item in restarted.deliveries("session")],
                [
                    ("message-1", "uncertain", "identical"),
                    ("message-2", "uncertain", "identical"),
                ],
            )
            self.assertEqual(
                restarted.reserve("rpc-command:session", "message-1", "1" * 64),
                {
                    "requestId": "message-1",
                    "status": "uncertain",
                    "errorCode": "outcome_unknown",
                },
            )
            self.assertTrue(restarted.dismiss_delivery("session", "message-1"))
            self.assertEqual(
                [item["requestId"] for item in restarted.deliveries("session")],
                ["message-2"],
            )

    def test_stale_temporary_file_does_not_block_future_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "requests.json"
            stale = path.with_name(".requests.json.collision.tmp")
            stale.write_text("abandoned")
            ledger = RequestLedger(path)

            with patch(
                "omp_deck._atomic_file.secrets.token_hex",
                side_effect=["collision", "fresh"],
            ):
                self.assertIsNone(
                    ledger.reserve(
                        "rpc-command:session",
                        "request-1",
                        "a" * 64,
                    )
                )

            self.assertTrue(stale.exists())
            self.assertTrue(path.exists())

    @unittest.skipIf(os.name == "nt", "Windows symlink creation requires elevated privileges")
    def test_symlinked_lock_is_rejected_without_touching_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            victim = root / "victim"
            victim.write_text("unrelated", encoding="utf-8")
            victim.chmod(0o644)
            ledger = RequestLedger(root / "requests.json")
            ledger.lock_path.symlink_to(victim)

            with self.assertRaises(OSError):
                ledger.reserve("rpc-launch", "request-1", "a" * 64)

            self.assertTrue(ledger.lock_path.is_symlink())
            self.assertEqual(victim.stat().st_mode & 0o777, 0o644)
            self.assertEqual(victim.read_text(encoding="utf-8"), "unrelated")


if __name__ == "__main__":
    unittest.main()
