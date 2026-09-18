from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.release_lib import worker

from omp_deck.auth import AuthenticationFailed, AuthError, AuthManager


class Native:
    def __init__(self, status, rollback=None):
        self.status = status
        self.rollback = rollback or {"version": 1, "state": "rolledback"}
        self.actions = []

    def execute(self, action, request):
        self.actions.append(action)
        return self.rollback if action == "rollback" else self.status


@unittest.skipIf(os.name == "nt", "release worker supervises POSIX services")
class ReleaseWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.root = base / "release"
        self.root.mkdir(mode=0o700)
        self.state_root = base / "state"
        self.request = {
            "version": 1,
            "transaction": "release-1",
            "root": str(self.root),
            "kind": "node",
            "profile": {"state_root": str(self.state_root), "port": "9000"},
            "candidate": {"wheelVersion": "1.2.3", "revision": "abc"},
            "rollback": {},
            "origin": "https://deck.example",
            "_worker_argv": ["python", "worker.pyz"],
        }
        worker._paths(self.request)

    def test_cleanup_revokes_only_maintenance_device_and_checks_it_is_invalid(self):
        existing = AuthManager(self.state_root).authenticate_or_issue(None)
        path = worker._mint(self.request, self.root / "guard")
        maintenance = json.loads(path.read_text())["credential"]

        worker.cleanup(self.request)

        restarted = AuthManager(self.state_root)
        self.assertEqual(restarted.authenticate(existing.credential).device_id, existing.device.device_id)
        with self.assertRaises(AuthenticationFailed):
            restarted.authenticate(maintenance)
        self.assertFalse(path.exists())

    def test_maintenance_capacity_failure_preserves_existing_device(self):
        manager = AuthManager(self.state_root, max_devices=1)
        existing = manager.authenticate_or_issue(None)
        with self.assertRaisesRegex(AuthError, "capacity"):
            manager.authenticate_or_issue(None, evict_oldest=False)
        self.assertEqual(
            AuthManager(self.state_root).authenticate(existing.credential).device_id,
            existing.device.device_id,
        )

    def test_arm_returns_only_after_matching_fresh_readiness(self):
        class Process:
            pass

        def spawn(argv, **kwargs):
            request = json.loads((self.root / "guard" / "watchdog-request.json").read_text())
            worker._private_json(
                self.root / "watchdog-ready.json",
                {
                    "version": 1,
                    "transaction": self.request["transaction"],
                    "pid": 123,
                    "nonce": request["_watchdog_nonce"],
                    "readyAt": int(time.time()),
                },
            )
            return Process()

        with patch.object(worker.subprocess, "Popen", side_effect=spawn):
            result = worker.arm(self.request)

        self.assertEqual(result, {"version": 1, "state": "prepared", "watchdogPid": 123})
        self.assertTrue((self.root / "guard" / "credential.json").is_file())

    def test_watchdog_cleans_up_abandoned_prepared_guard(self):
        worker._private_json(
            self.root / "guard" / "worker-state.json",
            {"version": 1, "transaction": "release-1", "state": "armed", "armDeadline": 1},
        )
        request = dict(self.request, _watchdog_nonce="a" * 64)
        native = Native({"version": 1, "state": "prepared"})

        with patch.object(worker, "_native", return_value=native), patch.object(worker.time, "time", return_value=2):
            result = worker.watchdog(request)

        self.assertEqual(result["state"], "absent")
        self.assertFalse((self.root / "watchdog-ready.json").exists())

    def test_watchdog_rolls_back_expired_pending_release(self):
        worker._private_json(
            self.root / "guard" / "worker-state.json",
            {"version": 1, "transaction": "release-1", "state": "armed", "armDeadline": 999},
        )
        native = Native({"version": 1, "state": "pending", "deadline": 10.5})
        request = dict(self.request, _watchdog_nonce="a" * 64)

        with patch.object(worker, "_native", return_value=native), patch.object(worker.time, "time", return_value=11):
            result = worker.watchdog(request)

        self.assertEqual(result["state"], "rolledback")
        self.assertEqual(native.actions, ["status", "rollback"])
        self.assertFalse((self.root / "watchdog-ready.json").exists())


if __name__ == "__main__":
    unittest.main()
