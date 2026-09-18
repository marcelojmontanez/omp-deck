from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tools.release_lib import coordinator


@unittest.skipIf(os.name == "nt", "coordinator release guard targets Linux services")
class CoordinatorReleaseTests(unittest.TestCase):
    def test_unit_parser_requires_one_exact_execstart(self) -> None:
        expected = [
            "/srv/releases/old/bin/python",
            "-m",
            "omp_deck",
            "coordinator",
            "serve",
            "--config",
            "/etc/deck.json",
        ]
        payload = b"[Service]\nExecStart=/srv/releases/old/bin/python -m omp_deck coordinator serve --config /etc/deck.json\nRestart=on-failure\n"
        commands = coordinator._unit_commands(payload)
        self.assertEqual([item[2] for item in commands], [expected])
        duplicate = payload + payload.splitlines(keepends=True)[1]
        self.assertEqual(len(coordinator._unit_commands(duplicate)), 2)

    def test_absolute_path_rejects_traversal_and_relative_input(self) -> None:
        with self.assertRaisesRegex(coordinator.CoordinatorError, "absolute"):
            coordinator._path("relative/release", "root")
        with self.assertRaisesRegex(coordinator.CoordinatorError, "traversal"):
            coordinator._path("/safe/../escaped", "root")

    def test_context_rejects_root_outside_transaction_rollback_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            outside = base / "outside"
            outside.mkdir()
            profile = {"rollback_root": base / "rollbacks"}
            request = {
                "version": 1,
                "kind": "coordinator",
                "transaction": "release-1",
                "root": str(outside),
            }
            with mock.patch.object(coordinator, "_profile", return_value=profile):
                with self.assertRaisesRegex(coordinator.CoordinatorError, "outside the rollback root"):
                    coordinator._context(request)

    def test_verification_requires_exact_fresh_completed_checks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "verification.json"
            value = {
                "version": 1,
                "transaction": "release-1",
                "completedAt": int(time.time()),
                "checks": ["local-health", "local-identity", "local-auth", "browser-smoke"],
            }
            path.write_text(json.dumps(value))
            path.chmod(0o600)
            coordinator._verification(path, {"transaction": "release-1"})
            value["checks"][-1] = "invented-check"
            path.write_text(json.dumps(value))
            path.chmod(0o600)
            with self.assertRaisesRegex(coordinator.CoordinatorError, "stale or mismatched"):
                coordinator._verification(path, {"transaction": "release-1"})

    def test_rollback_refuses_unrelated_unit_without_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            unit = root / "coordinator.service"
            backup = root / "coordinator-unit.backup"
            manifest = root / "coordinator-state.json"
            unit.write_bytes(b"unrelated\n")
            backup.write_bytes(b"original\n")
            unit.chmod(0o600)
            backup.chmod(0o600)
            state = {
                "version": 1,
                "decision": "pending",
                "deadline": time.time() + 60,
                "oldUnitSha256": coordinator.hashlib.sha256(b"original\n").hexdigest(),
                "newUnitSha256": coordinator.hashlib.sha256(b"candidate\n").hexdigest(),
                "unitMode": 0o600,
                "rollback": {"wheelVersion": "1.0", "revision": "a" * 40},
            }
            profile = {"unit": unit}
            with mock.patch.object(coordinator, "_guard"), mock.patch.object(coordinator, "_systemctl") as systemctl:
                with self.assertRaisesRegex(coordinator.CoordinatorError, "unrelated changes"):
                    coordinator._rollback(profile, state, manifest)
            systemctl.assert_not_called()
            saved = json.loads(manifest.read_text())
            self.assertEqual(saved["decision"], "rollback_failed")
            self.assertIsNone(saved["deadline"])


if __name__ == "__main__":
    unittest.main()
