import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.release_lib import orchestrator
from tools.release_lib.orchestrator import Release
from tools.release_lib.storage import ReleaseError, directory, load, save

TOOLS = Path(__file__).resolve().parents[1] / "tools"


class FleetTransport:
    states = {}
    calls = []
    failures = set()
    identity = {"version": "1.5.99+g" + "a" * 40, "revision": "a" * 40}

    def __init__(self, target, transaction, local_root, repository):
        self.name = target["id"]
        self.transaction = transaction

    def action(self, action):
        self.calls.append((self.name, action))
        state = self.states.get(self.transaction, "prepared")
        if action == "deploy":
            state = "pending"
        if action == "rollback":
            state = "rolledback"
        self.states[self.transaction] = state
        return {"version": 1, "state": state, "installed": self.identity, "pluginActivation": "unchanged"}

    def verify(self):
        self.calls.append((self.name, "verify"))
        if self.name in self.failures:
            raise ReleaseError("unhealthy candidate")
        self.states[self.transaction] = "verified"
        return {"state": "verified"}


class ReleaseOrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.config = {"state_root": str(self.root / "state"), "targets": []}
        self.release = Release(self.config, TOOLS.parent, "test-release", transport=FleetTransport)
        directory(self.release.root)
        self.release.journal = {
            "version": 1,
            "transaction": "test-release",
            "state": "prepared",
            "candidate": {
                "wheelVersion": FleetTransport.identity["version"],
                "revision": "a" * 40,
                "wheelPath": "/retained/candidate.whl",
            },
            "targets": [self.entry("mac"), self.entry("linux"), self.entry("coordinator")],
        }
        self.release.save()
        FleetTransport.states = {}
        FleetTransport.calls = []
        FleetTransport.failures = set()
        self.browser = patch.object(orchestrator, "run_browser")
        self.browser.start()
        self.addCleanup(self.browser.stop)

    def entry(self, name):
        return {
            "id": name,
            "transaction": f"test-release-{name}-1",
            "state": "prepared",
            "attempt": 1,
            "target": {
                "id": name,
                "kind": "coordinator" if name == "coordinator" else "node",
                "profile": {"tool_root": "/retained/current", "releases_root": "/retained/releases"},
            },
        }

    def test_failed_verification_restores_target_and_stops_before_other_nodes(self):
        FleetTransport.failures = {"mac"}
        with self.assertRaises(ReleaseError):
            self.release.deploy()
        self.assertEqual(FleetTransport.states["test-release-mac-1"], "rolledback")
        self.assertNotIn(("linux", "deploy"), FleetTransport.calls)
        self.assertNotIn(("coordinator", "deploy"), FleetTransport.calls)
        self.assertIn(("mac", "cleanup"), FleetTransport.calls)
        self.assertEqual(load(self.release.path)["state"], "rolledback")

    def test_resume_verifies_pending_cutover_without_repeating_it(self):
        FleetTransport.states = {"test-release-mac-1": "verified", "test-release-linux-1": "pending"}
        result = self.release.deploy(resume=True)
        self.assertEqual(result["state"], "verified")
        self.assertNotIn(("mac", "deploy"), FleetTransport.calls)
        self.assertNotIn(("linux", "deploy"), FleetTransport.calls)
        self.assertLess(
            FleetTransport.calls.index(("linux", "verify")), FleetTransport.calls.index(("coordinator", "deploy"))
        )
        self.assertIsNone(load(self.root / "state/fleet.json")["active"])

    def test_resume_escapes_partial_immutable_preparation(self):
        self.release.journal["state"] = "prepare_failed"
        self.release.journal["targets"][0]["state"] = "prepare_failed"
        partial = self.release.journal["targets"][0]["transaction"]

        def prepare_target(entry, fleet):
            if entry["transaction"] == partial:
                raise FileExistsError("partial immutable snapshots already exist")
            return {"state": "prepared"}

        with patch.object(self.release, "prepare_target", side_effect=prepare_target):
            result = self.release.deploy(resume=True)
        self.assertEqual(result["state"], "verified")
        self.assertEqual(
            [name for name, action in FleetTransport.calls if action == "deploy"], ["mac", "linux", "coordinator"]
        )

    def test_concurrent_release_cannot_supersede_unresolved_rollout(self):
        directory(self.root / "state")
        save(self.root / "state/fleet.json", {"version": 1, "active": "another", "owner": None, "targets": {}})
        with self.assertRaisesRegex(ReleaseError, "unfinished"):
            self.release.deploy()
        self.assertEqual(FleetTransport.calls, [])

    def test_status_does_not_rewrite_an_active_release_journal(self):
        before = self.release.path.read_bytes()
        FleetTransport.states["test-release-mac-1"] = "pending"
        self.assertEqual(self.release.status()["targets"][0]["state"], "pending")
        self.assertEqual(self.release.path.read_bytes(), before)

    def test_verified_version_drift_stops_without_overwriting_target(self):
        FleetTransport.states["test-release-mac-1"] = "verified"
        old = FleetTransport.identity
        FleetTransport.identity = {"version": "unexpected", "revision": "b" * 40}
        self.addCleanup(setattr, FleetTransport, "identity", old)
        with self.assertRaises(ReleaseError):
            self.release.deploy(resume=True)
        self.assertNotIn(("mac", "deploy"), FleetTransport.calls)
        self.assertNotIn(("mac", "rollback"), FleetTransport.calls)
        self.assertNotIn(("linux", "deploy"), FleetTransport.calls)
