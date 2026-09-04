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

            pending = RequestLedger(path).reserve(
                "rpc-command:session", "request-1", "a" * 64
            )
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
            first.complete(
                "rpc-command:session", "request-1", "a" * 64, accepted
            )
            self.assertEqual(
                RequestLedger(path).reserve(
                    "rpc-command:session", "request-1", "a" * 64
                ),
                accepted,
            )
            with self.assertRaises(RequestConflictError):
                RequestLedger(path).reserve(
                    "rpc-command:session", "request-1", "b" * 64
                )

            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(first.lock_path.stat().st_mode & 0o777, 0o600)

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
