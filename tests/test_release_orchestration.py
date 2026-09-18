import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from tools.release_lib import orchestrator
from tools.release_lib.build import CONTRACT_FILES, artifact, compatible
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


class StagingTransport:
    staged = False

    def __init__(self, target, transaction, local_root, repository):
        self.root = str(local_root / "remote")

    def stage(self, bundle, wheel, rollback, request):
        type(self).staged = True

    def action(self, action):
        return {"state": "prepared"}


class ReleaseStorageCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def wheel(self, name, *, readable=None, writable=None, ledger="ledger") -> Path:
        path = self.root / f"{name}.whl"
        revision = ("a" if name == "candidate" else "b") * 40
        package = ""
        if readable is not None:
            package = (
                "def storage_compatibility():\n"
                f"    return {{'requests': {{'read': {readable!r}, 'write': {writable!r}}}}}\n"
            )
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("omp_deck/__init__.py", package)
            archive.writestr("omp_deck/request_ledger.py", ledger)
            archive.writestr("omp_deck/plugin/index.js", name)
            for contract in CONTRACT_FILES:
                archive.writestr(contract, "stable contract\n")
            archive.writestr(
                "omp_deck-1.0.dist-info/METADATA",
                f"Name: omp-deck\nVersion: 1.0+g{revision}\n",
            )
        return path

    def release(self, candidate: Path, rollback: Path) -> tuple[Release, dict, dict]:
        config = {"state_root": str(self.root / "state"), "targets": []}
        release = Release(config, self.root, "storage-release", transport=StagingTransport)
        directory(release.root)
        candidate_artifact = artifact(candidate)
        release.journal = {
            "version": 1,
            "transaction": "storage-release",
            "state": "preparing",
            "candidate": candidate_artifact,
            "targets": [],
        }
        entry = {
            "id": "node",
            "transaction": "storage-release-node-1",
            "target": {
                "id": "node",
                "kind": "node",
                "profile": {"tool_root": "/retained/tool"},
                "work_root": "/retained/work",
                "origin": "https://deck.example",
            },
        }
        fleet = {
            "targets": {
                "node": {
                    "artifact": artifact(rollback),
                    "tool_root": "/retained/tool",
                }
            }
        }
        return release, entry, fleet

    def test_legacy_rollback_without_storage_api_is_rejected_before_remote_staging(self):
        candidate = self.wheel("candidate", readable=[1, 2, 3], writable=3, ledger="schema 3")
        rollback = self.wheel("rollback", ledger="schema 2")
        release, entry, fleet = self.release(candidate, rollback)
        StagingTransport.staged = False
        with self.assertRaisesRegex(ReleaseError, "incompatible storage schema"):
            release.prepare_target(entry, fleet)
        self.assertFalse(StagingTransport.staged)

    def test_mutually_readable_storage_versions_allow_different_ledger_implementations(self):
        candidate = self.wheel("candidate", readable=[1, 2, 3], writable=3, ledger="schema 3")
        rollback = self.wheel("rollback", readable=[1, 2, 3], writable=2, ledger="schema 2 reader")
        compatible(candidate, rollback)

    def test_each_artifact_must_read_the_other_artifacts_write_version(self):
        candidate = self.wheel("candidate", readable=[3], writable=3)
        rollback = self.wheel("rollback", readable=[2, 3], writable=2)
        with self.assertRaisesRegex(ReleaseError, "incompatible storage schema"):
            compatible(candidate, rollback)


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
