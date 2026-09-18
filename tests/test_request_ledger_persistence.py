import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from omp_deck import request_ledger
from omp_deck.request_ledger import RequestLedger


class RequestLedgerPersistenceTests(unittest.TestCase):
    @staticmethod
    def recovery(message="payload", images=None):
        return {
            "kind": "message",
            "sessionId": "session",
            "action": "sendMessage",
            "message": message,
            "images": [] if images is None else images,
        }

    def test_backward_wall_time_cannot_lose_native_completion_or_dismissal(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = [100.0]
            ledger = RequestLedger(Path(directory) / "requests.json", clock=lambda: clock[0])
            ledger.reserve("rpc-command:session", "request-1", "a" * 64, recovery=self.recovery())

            clock[0] = 99.0
            outcome = {"requestId": "request-1", "status": "accepted", "acceptance": "accepted"}
            ledger.complete("rpc-command:session", "request-1", "a" * 64, outcome)
            self.assertEqual(ledger.lookup("rpc-command:session", "request-1", "a" * 64), outcome)
            self.assertTrue(ledger.dismiss_delivery("session", "request-1"))
            self.assertEqual(ledger.deliveries("session"), [])

    def test_dismissed_uncertainty_uses_bounded_dedup_tombstone_capacity(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = RequestLedger(Path(directory) / "requests.json", max_records=1)
            ledger.reserve("rpc-command:session", "old", "a" * 64, recovery=self.recovery("old"))
            ledger.mark_uncertain("rpc-command:session", "old", "a" * 64)
            self.assertTrue(ledger.dismiss_delivery("session", "old"))

            self.assertIsNone(ledger.reserve("rpc-command:session", "new", "b" * 64, recovery=self.recovery("new")))
            self.assertEqual(
                ledger.lookup("rpc-command:session", "old", "a" * 64),
                {"requestId": "old", "status": "uncertain", "errorCode": "outcome_unknown"},
            )
            ledger.mark_uncertain("rpc-command:session", "new", "b" * 64)
            self.assertTrue(ledger.dismiss_delivery("session", "new"))
            self.assertIsNone(ledger.lookup("rpc-command:session", "old", "a" * 64))
            self.assertEqual(
                ledger.lookup("rpc-command:session", "new", "b" * 64),
                {"requestId": "new", "status": "uncertain", "errorCode": "outcome_unknown"},
            )

    def test_expired_uncertainty_reclaims_active_quota_without_replaying_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = [100.0]
            ledger = RequestLedger(Path(directory) / "requests.json", max_records=1, clock=lambda: clock[0])
            ledger.reserve("rpc-command:session", "old", "a" * 64, recovery=self.recovery())
            ledger.mark_uncertain("rpc-command:session", "old", "a" * 64)
            clock[0] += 8 * 24 * 60 * 60

            self.assertIsNone(ledger.reserve("rpc-command:session", "new", "b" * 64))
            self.assertEqual(
                ledger.reserve("rpc-command:session", "old", "a" * 64),
                {"requestId": "old", "status": "uncertain", "errorCode": "outcome_unknown"},
            )

    def test_live_native_admission_survives_cross_process_dismissal_and_clock_jump(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "requests.json"
            ledger = RequestLedger(path, max_records=1, clock=lambda: 100.0)
            with ledger.admission("rpc-command:session", "live", "a" * 64, recovery=self.recovery()) as previous:
                self.assertIsNone(previous)
                script = """
import json, sys
from omp_deck.request_ledger import RequestLedger, RequestLedgerError, RequestConflictError
ledger = RequestLedger(sys.argv[1], max_records=1, clock=lambda: 8 * 24 * 60 * 60)
result = {"state": ledger.deliveries("session")[0]["state"]}
try:
    ledger.dismiss_delivery("session", "live")
except RequestConflictError:
    result["dismissBlocked"] = True
try:
    with ledger.admission("rpc-command:session", "other", "b" * 64) as previous:
        result["executedOther"] = previous is None
except RequestLedgerError:
    result["capacityReserved"] = True
print(json.dumps(result))
"""
                probe = subprocess.run(
                    [sys.executable, "-c", script, str(path)],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                self.assertEqual(probe.returncode, 0, probe.stderr)
                self.assertEqual(
                    json.loads(probe.stdout),
                    {"state": "submitting", "dismissBlocked": True, "capacityReserved": True},
                )
                accepted = {"requestId": "live", "status": "accepted"}
                ledger.complete("rpc-command:session", "live", "a" * 64, accepted)
            self.assertEqual(ledger.lookup("rpc-command:session", "live", "a" * 64), accepted)
            self.assertTrue(ledger.dismiss_delivery("session", "live"))

    def test_process_exit_releases_admission_without_forgetting_uncertain_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "requests.json"
            script = """
import os, sys
from omp_deck.request_ledger import RequestLedger
ledger = RequestLedger(sys.argv[1], max_records=1)
recovery = {"kind": "message", "sessionId": "session", "action": "sendMessage", "message": "recover me", "images": []}
with ledger.admission("rpc-command:session", "crashed", "a" * 64, recovery=recovery):
    os._exit(17)
"""
            crashed = subprocess.run(
                [sys.executable, "-c", script, str(path)],
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.assertEqual(crashed.returncode, 17, crashed.stderr)
            recovered = RequestLedger(path, max_records=1)
            receipt = recovered.deliveries("session")[0]
            self.assertEqual((receipt["state"], receipt["message"]), ("uncertain", "recover me"))
            self.assertTrue(recovered.dismiss_delivery("session", "crashed"))
            with recovered.admission("rpc-command:session", "next", "b" * 64) as previous:
                self.assertIsNone(previous)
                recovered.complete(
                    "rpc-command:session",
                    "next",
                    "b" * 64,
                    {"requestId": "next", "status": "accepted"},
                )

    def test_original_completion_growth_boundary_persists_known_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            probe = RequestLedger(root / "probe" / "requests.json", clock=lambda: 100.0)
            probe.reserve("rpc-command:session", "request-1", "a" * 64, recovery=self.recovery())
            admitted_size = probe.path.stat().st_size

            ledger = RequestLedger(root / "ledger" / "requests.json", clock=lambda: 100.0)
            outcome = {"requestId": "request-1", "status": "failed", "errorCode": "command"}
            with patch.object(request_ledger, "_MAX_FILE_BYTES", admitted_size):
                self.assertIsNone(
                    ledger.reserve("rpc-command:session", "request-1", "a" * 64, recovery=self.recovery())
                )
                ledger.complete("rpc-command:session", "request-1", "a" * 64, outcome)
                self.assertEqual(ledger.lookup("rpc-command:session", "request-1", "a" * 64), outcome)

    def test_admission_reserves_the_largest_terminal_metadata_outcome(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            probe = RequestLedger(root / "probe" / "requests.json", clock=lambda: 100.0)
            probe.reserve("rpc-command:session", "request-1", "a" * 64, recovery=self.recovery())
            pending = json.loads(probe.path.read_text(encoding="utf-8"))
            reserved_size = len(RequestLedger._encode(request_ledger.capacity_projection(pending)))

            ledger = RequestLedger(root / "ledger" / "requests.json", clock=lambda: 100.0)
            outcome = {
                "requestId": "request-1",
                "status": "cancelled",
                "errorCode": "x" * 128,
                "result": "stale",
                "sessionId": "x" * 128,
                "created": False,
            }
            with patch.object(request_ledger, "_MAX_METADATA_BYTES", reserved_size):
                self.assertIsNone(
                    ledger.reserve(
                        "rpc-command:session",
                        "request-1",
                        "a" * 64,
                        recovery=self.recovery(),
                    )
                )
                ledger.complete("rpc-command:session", "request-1", "a" * 64, outcome)
                self.assertEqual(
                    ledger.lookup("rpc-command:session", "request-1", "a" * 64),
                    outcome,
                )

    def test_metadata_lookup_does_not_decode_retained_image_payloads(self):
        encoded = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aM1sAAAAASUVORK5CYII="
        image = {"type": "image", "data": encoded, "mimeType": "image/png"}
        with tempfile.TemporaryDirectory() as directory:
            ledger = RequestLedger(Path(directory) / "requests.json")
            ledger.reserve("rpc-command:session", "image", "a" * 64, recovery=self.recovery("", [image]))
            if os.name != "nt":
                recovery_root = ledger.path.with_name(".requests.json.recoveries")
                payloads = list(recovery_root.glob("*.json"))
                self.assertEqual(recovery_root.stat().st_mode & 0o777, 0o700)
                self.assertEqual(len(payloads), 1)
                self.assertEqual(payloads[0].stat().st_mode & 0o777, 0o600)
            with patch(
                "omp_deck._request_ledger_schema.validated_image_content",
                side_effect=AssertionError("metadata lookup decoded a recovery image"),
            ):
                self.assertEqual(
                    ledger.lookup("rpc-command:session", "image", "a" * 64),
                    {"requestId": "image", "status": "uncertain", "errorCode": "outcome_unknown"},
                )

    def test_interrupted_v2_migration_retries_without_losing_recovery_or_dedup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "requests.json"
            legacy = {
                "version": 2,
                "records": [
                    {
                        "scope": "rpc-command:session",
                        "requestId": "request-1",
                        "fingerprint": "a" * 64,
                        "state": "pending",
                        "outcome": None,
                        "createdAt": 100_000,
                        "updatedAt": 100_000,
                        "recovery": {
                            **self.recovery("recover me"),
                            "state": "uncertain",
                            "errorCode": "outcome_unknown",
                        },
                    }
                ],
            }
            path.write_text(json.dumps(legacy, separators=(",", ":")), encoding="utf-8")
            path.chmod(0o600)
            interrupted = RequestLedger(path, clock=lambda: 100.0)
            with patch.object(interrupted._storage, "replace", side_effect=OSError("interrupted")):
                with self.assertRaisesRegex(OSError, "interrupted"):
                    interrupted.deliveries("session")
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["version"], 2)

            restarted = RequestLedger(path, clock=lambda: 100.0)
            self.assertEqual(
                [(item["requestId"], item["state"], item["message"]) for item in restarted.deliveries("session")],
                [("request-1", "uncertain", "recover me")],
            )
            self.assertEqual(
                restarted.lookup("rpc-command:session", "request-1", "a" * 64),
                {"requestId": "request-1", "status": "uncertain", "errorCode": "outcome_unknown"},
            )
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["version"], 3)

    def test_v1_fixture_migrates_without_losing_completed_deduplication(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "requests.json"
            outcome = {"requestId": "request-1", "status": "failed", "errorCode": "command"}
            legacy = {
                "version": 1,
                "records": [
                    {
                        "scope": "rpc-command:session",
                        "requestId": "request-1",
                        "fingerprint": "a" * 64,
                        "state": "complete",
                        "outcome": outcome,
                    }
                ],
            }
            path.write_text(json.dumps(legacy, separators=(",", ":")), encoding="utf-8")
            path.chmod(0o600)

            ledger = RequestLedger(path, clock=lambda: 100.0)
            self.assertEqual(
                ledger.reserve("rpc-command:session", "request-1", "a" * 64),
                outcome,
            )
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["version"], 3)

    def test_owned_atomic_remnant_is_reclaimed_but_hostile_symlink_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "requests.json"
            stale = root / f".requests.json.{'a' * 32}.tmp"
            stale.write_text("private recovery marker", encoding="utf-8")
            stale.chmod(0o600)
            ledger = RequestLedger(path)
            ledger.reserve("rpc-command:session", "request-1", "a" * 64)
            self.assertFalse(stale.exists())

            if os.name == "nt":
                return
            victim = root / "victim"
            victim.write_text("unrelated", encoding="utf-8")
            hostile = root / f".requests.json.{'b' * 32}.tmp"
            hostile.symlink_to(victim)
            with self.assertRaises(OSError):
                ledger.lookup("rpc-command:session", "request-1", "a" * 64)
            self.assertTrue(hostile.is_symlink())
            self.assertEqual(victim.read_text(encoding="utf-8"), "unrelated")

    def test_unreferenced_owned_recovery_sidecar_is_reclaimed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "requests.json"
            recovery_root = path.with_name(".requests.json.recoveries")
            recovery_root.mkdir(mode=0o700)
            orphan = recovery_root / f"{'a' * 64}.json"
            orphan.write_text("orphaned private body", encoding="utf-8")
            orphan.chmod(0o600)

            ledger = RequestLedger(path)
            self.assertIsNone(ledger.lookup("rpc-command:session", "missing", "b" * 64))
            self.assertFalse(orphan.exists())

            if os.name != "nt":
                victim = Path(directory) / "victim"
                victim.write_text("unrelated", encoding="utf-8")
                hostile = recovery_root / f"{'c' * 64}.json"
                hostile.symlink_to(victim)
                with self.assertRaises(OSError):
                    ledger.lookup("rpc-command:session", "missing", "b" * 64)
                self.assertTrue(hostile.is_symlink())
                self.assertEqual(victim.read_text(encoding="utf-8"), "unrelated")


if __name__ == "__main__":
    unittest.main()
