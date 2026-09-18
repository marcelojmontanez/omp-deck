"""Isolated fake-service recovery harnesses.

Safe CLI lifecycle smoke:
python -m unittest \
  tests.test_release_recovery.OperatorRecoveryCliTests.test_actual_cli_resume_releases_prebuild_journal_with_reused_pid

The smoke creates only temporary configuration/state and never stages or contacts a target.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tools.release_lib import coordinator, node, node_plugin, node_rollback
from tools.release_lib.node_common import ReleaseError as NodeReleaseError
from tools.release_lib.orchestrator import Release
from tools.release_lib.storage import ReleaseError, directory, load, save

ROOT = Path(__file__).resolve().parents[1]
ROLLBACK_IDENTITY = {"version": "1", "revision": "a" * 40}
CANDIDATE_IDENTITY = {"version": "2", "revision": "b" * 40}


class NodeRollbackRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.guard = self.root / "guard"
        self.backup = self.guard / "snapshots/uv-tool-environment"
        self.backup.mkdir(parents=True)
        self.guard.chmod(0o700)
        (self.backup / "identity").write_text("rollback")
        self.plugin_backup = self.guard / "snapshots/plugin"
        self.plugin_backup.mkdir()
        (self.plugin_backup / "package.json").write_text('{"name":"omp-deck"}')
        self.tool = self.root / "tool"
        self.auth = self.root / "auth.json"
        self.auth.write_text("durable-auth")
        self.auth.chmod(0o600)
        self.profile = SimpleNamespace(tool_root=self.tool, auth=self.auth, plugin_expected=self.root / "legacy-plugin")

    def manifest(self, phase: str | None, *, state: str = "rollback_failed") -> dict:
        value = {
            "version": 1,
            "transaction": "recovery-1",
            "state": state,
            "deadline": 1,
            "touched": True,
            "candidate": {},
            "rollback": {"wheelVersion": "1", "revision": "a" * 40, "pluginSha256": "c" * 64},
            "constraints": str(self.root / "constraints"),
            "constraintsSha256": "d" * 64,
            "originals": {
                "environmentBackup": str(self.backup),
                "pluginBackup": str(self.plugin_backup),
                "serviceEnvironment": {},
                "serviceEnabled": True,
            },
            "cutover": {"registryStable": []},
            "loopbackHTTP": None,
        }
        if phase is not None:
            value["rollbackPhase"] = phase
            value["rollbackEvidence"] = {
                "authSha256": hashlib.sha256(self.auth.read_bytes()).hexdigest(),
                "registryStable": [],
            }
        return value

    @staticmethod
    def environment(path: Path, identity: str) -> None:
        path.mkdir()
        (path / "identity").write_text(identity)

    def effects(self, service: dict[str, bool]) -> ExitStack:
        stack = ExitStack()
        self.addCleanup(stack.close)

        def identity(_profile, environment):
            marker = (environment / "identity").read_text()
            return ROLLBACK_IDENTITY if marker == "rollback" else CANDIDATE_IDENTITY

        stack.enter_context(patch.object(node, "_guard_files"))
        stack.enter_context(patch.object(node, "_verify_staged"))
        stack.enter_context(patch.object(node, "_environment_identity", side_effect=identity))
        stack.enter_context(patch.object(node, "_installed", side_effect=lambda _p: identity(_p, self.tool)))
        stack.enter_context(patch.object(node, "running", side_effect=lambda _p: service["running"]))
        stack.enter_context(patch.object(node_rollback, "quiescent", side_effect=lambda _p: not service["running"]))
        stack.enter_context(patch.object(node, "stop", side_effect=lambda _p, _pid: service.update(running=False)))
        stack.enter_context(patch.object(node, "start", side_effect=lambda _p: service.update(running=True)))
        stack.enter_context(patch.object(node_plugin, "restore_plugin", return_value=self.root / "restored-plugin"))
        stack.enter_context(patch.object(node, "health"))
        stack.enter_context(patch.object(node, "service_enabled", return_value=True))
        stack.enter_context(patch.object(node, "registry", return_value=({}, [])))
        stack.enter_context(
            patch.object(
                node,
                "_admit",
                side_effect=lambda *_args: _admission({"pid": 123, "registryStable": []}),
            )
        )
        return stack

    def arrange_phase(self, phase: str) -> dict[str, bool]:
        failed = self.root / ".tool.failed-recovery-1"
        staging = self.root / ".tool.restore-recovery-1"
        service = {"running": phase == "service-started"}
        if phase in {"stopping-service", "service-stopped", "copying-environment", "environment-staged"}:
            self.environment(self.tool, "candidate")
        if phase == "copying-environment":
            self.environment(staging, "partial")
        elif phase == "environment-staged":
            self.environment(staging, "rollback")
        elif phase == "swapping-after-move":
            self.environment(failed, "candidate")
            self.environment(staging, "rollback")
        elif phase in {"swapping-after-restore", "environment-restored"}:
            self.environment(failed, "candidate")
            self.environment(self.tool, "rollback")
        elif phase in {"plugin-restored", "service-started"}:
            self.environment(self.tool, "rollback")
        elif phase == "swapping-after-restore-missing-original":
            self.environment(self.tool, "rollback")
        return service

    def test_every_durable_environment_swap_phase_converges(self) -> None:
        cases = {
            "stopping-service": "stopping-service",
            "service-stopped": "service-stopped",
            "copying-environment": "copying-environment",
            "environment-staged": "environment-staged",
            "swapping-after-move": "swapping-environment",
            "swapping-after-restore": "swapping-environment",
            "swapping-after-restore-missing-original": "swapping-environment",
            "environment-restored": "environment-restored",
            "plugin-restored": "plugin-restored",
            "service-started": "service-started",
        }
        for fixture, persisted_phase in cases.items():
            with self.subTest(phase=fixture):
                for path in (self.tool, self.root / ".tool.failed-recovery-1", self.root / ".tool.restore-recovery-1"):
                    if path.exists():
                        __import__("shutil").rmtree(path)
                service = self.arrange_phase(fixture)
                manifest = self.manifest(persisted_phase)
                if fixture == "swapping-after-restore-missing-original":
                    restored = self.tool.stat(follow_symlinks=False)
                    manifest["rollbackEnvironmentEvidence"] = {
                        "device": restored.st_dev,
                        "inode": restored.st_ino,
                    }
                with self.effects(service):
                    node._rollback_locked(self.profile, self.root, manifest, None, "watchdog recovery")
                self.assertEqual((self.tool / "identity").read_text(), "rollback")
                self.assertEqual(manifest["state"], "rolledback")
                self.assertTrue(service["running"])
                self.assertFalse((self.root / ".tool.failed-recovery-1").exists())
                self.assertFalse((self.root / ".tool.restore-recovery-1").exists())

    def test_unproven_active_environment_is_preserved_even_with_rollback_identity(self) -> None:
        self.environment(self.tool, "rollback")
        before = self.tool.stat().st_ino
        manifest = self.manifest("swapping-environment")
        with self.effects({"running": False}), self.assertRaisesRegex(NodeReleaseError, "ambiguous incomplete"):
            node._rollback_locked(self.profile, self.root, manifest, None, "unsafe recovery")
        self.assertEqual(self.tool.stat().st_ino, before)
        self.assertEqual((self.tool / "identity").read_text(), "rollback")

    def test_legacy_owned_failed_move_without_active_environment_is_resumed(self) -> None:
        failed = self.root / ".tool.failed-recovery-1"
        self.environment(failed, "candidate")
        manifest = self.manifest(None, state="pending")
        service = {"running": False}
        with self.effects(service):
            node._rollback_locked(self.profile, self.root, manifest, None, "legacy recovery")
        self.assertEqual((self.tool / "identity").read_text(), "rollback")
        self.assertFalse(failed.exists())

    def test_verified_state_is_terminal_before_any_service_or_file_mutation(self) -> None:
        self.environment(self.tool, "candidate")
        manifest = self.manifest(None, state="verified")
        service = {"running": True}
        before = self.tool.stat().st_ino
        with self.effects(service), self.assertRaisesRegex(NodeReleaseError, "verified"):
            node._rollback_locked(self.profile, self.root, manifest, None, "stale watchdog")
        self.assertEqual(self.tool.stat().st_ino, before)
        self.assertEqual(manifest["state"], "verified")
        self.assertTrue(service["running"])

    def test_unrelated_backup_path_is_refused_without_deletion(self) -> None:
        unrelated = self.root / "unrelated"
        self.environment(unrelated, "user-work")
        manifest = self.manifest("service-stopped")
        manifest["originals"]["environmentBackup"] = str(unrelated)
        with self.effects({"running": False}), self.assertRaisesRegex(NodeReleaseError, "outside this transaction"):
            node._rollback_locked(self.profile, self.root, manifest, None, "unsafe")
        self.assertEqual((unrelated / "identity").read_text(), "user-work")

    def test_public_rollback_does_not_relabel_a_verified_manifest(self) -> None:
        manifest = self.manifest(None, state="verified")
        with (
            patch.object(node, "_context", return_value=(self.profile, self.root, "recovery-1")),
            patch.object(node, "lock", side_effect=lambda _path: _admission(None)),
            patch.object(node, "_manifest", return_value=manifest),
            patch.object(node, "_save") as save_manifest,
            self.assertRaisesRegex(NodeReleaseError, "verified"),
        ):
            node.rollback({"version": 1})
        save_manifest.assert_not_called()
        self.assertEqual(manifest["state"], "verified")


class CoordinatorRollbackRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.unit = self.root / "coordinator.service"
        self.backup = self.root / "coordinator-unit.backup"
        self.auth = self.root / "auth.json"
        self.old_unit = b"old unit\n"
        self.new_unit = b"new unit\n"
        self.unit.write_bytes(self.old_unit)
        self.backup.write_bytes(self.old_unit)
        self.auth.write_bytes(b"durable auth")
        for path in (self.unit, self.backup, self.auth):
            path.chmod(0o600)
        self.profile = {
            "unit": self.unit,
            "state_root": self.root,
            "tool_root": self.root / "old-environment",
            "service_name": "isolated.service",
        }

    def state(self) -> dict:
        return {
            "version": 1,
            "decision": "pending",
            "deadline": 1,
            "oldUnitSha256": hashlib.sha256(self.old_unit).hexdigest(),
            "newUnitSha256": hashlib.sha256(self.new_unit).hexdigest(),
            "unitMode": 0o600,
            "rollback": {"wheelVersion": "1", "revision": "a" * 40},
        }

    def run_recovery(self, service: dict[str, object], *, fail_first_restart: bool = False):
        calls: list[tuple[str, ...]] = []
        failed = False

        def systemctl(_profile, *arguments):
            nonlocal failed
            calls.append(arguments)
            if arguments[0] == "daemon-reload":
                service["loaded"] = self.unit.read_bytes()
            if arguments[0] == "restart":
                if fail_first_restart and not failed:
                    failed = True
                    raise coordinator.CoordinatorError("simulated process death before restart")
                identity = (
                    ROLLBACK_IDENTITY
                    if service.get("loaded", self.unit.read_bytes()) == self.old_unit
                    else CANDIDATE_IDENTITY
                )
                service.update(running=True, identity=identity)

        def live(_profile):
            if not service["running"]:
                raise coordinator.CoordinatorError("not running")
            return service["identity"]

        patches = (
            patch.object(coordinator, "_guard"),
            patch.object(coordinator, "_identity", return_value=ROLLBACK_IDENTITY),
            patch.object(coordinator, "_active", side_effect=lambda _p: service["running"]),
            patch.object(coordinator, "_health", side_effect=lambda _p: None),
            patch.object(coordinator, "_live_identity", side_effect=live),
            patch.object(coordinator, "_systemctl", side_effect=systemctl),
        )
        return calls, patches

    def test_old_unit_with_candidate_process_is_reloaded_restarted_and_live_verified(self) -> None:
        state = self.state()
        service = {"running": True, "identity": CANDIDATE_IDENTITY}
        calls, patches = self.run_recovery(service)
        with ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            result = coordinator._rollback(self.profile, state, self.root / "coordinator-state.json")
        self.assertEqual(calls, [("daemon-reload",), ("restart", "isolated.service")])
        self.assertEqual(result["installed"], ROLLBACK_IDENTITY)
        self.assertEqual(state["decision"], "rolledback")

    def test_candidate_unit_bytes_are_restored_before_service_restart(self) -> None:
        self.unit.write_bytes(self.new_unit)
        self.unit.chmod(0o600)
        state = self.state()
        service = {"running": True, "identity": CANDIDATE_IDENTITY}
        calls, patches = self.run_recovery(service)
        with ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            coordinator._rollback(self.profile, state, self.root / "coordinator-state.json")
        self.assertEqual(self.unit.read_bytes(), self.old_unit)
        self.assertEqual(calls, [("daemon-reload",), ("restart", "isolated.service")])

    def test_old_live_process_with_candidate_unit_cached_still_reloads_old_definition(self) -> None:
        self.unit.write_bytes(self.new_unit)
        self.unit.chmod(0o600)
        state = self.state()
        service = {"running": True, "identity": ROLLBACK_IDENTITY, "loaded": self.new_unit}
        calls, patches = self.run_recovery(service)
        with ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            result = coordinator._rollback(self.profile, state, self.root / "coordinator-state.json")
        self.assertEqual(calls, [("daemon-reload",)])
        self.assertEqual(self.unit.read_bytes(), self.old_unit)
        self.assertEqual(result["installed"], ROLLBACK_IDENTITY)
        self.assertEqual(service["loaded"], self.old_unit)

    def test_stopped_service_is_restarted_even_when_old_unit_is_already_present(self) -> None:
        state = self.state()
        service = {"running": False, "identity": CANDIDATE_IDENTITY}
        calls, patches = self.run_recovery(service)
        with ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            coordinator._rollback(self.profile, state, self.root / "coordinator-state.json")
        self.assertIn(("restart", "isolated.service"), calls)
        self.assertTrue(service["running"])

    def test_restart_failure_phase_is_durable_and_retry_converges(self) -> None:
        state = self.state()
        service = {"running": True, "identity": CANDIDATE_IDENTITY}
        calls, patches = self.run_recovery(service, fail_first_restart=True)
        with ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            with self.assertRaisesRegex(coordinator.CoordinatorError, "simulated"):
                coordinator._rollback(self.profile, state, self.root / "coordinator-state.json")
            self.assertEqual(state["decision"], "rollback_failed")
            self.assertEqual(state["rollbackPhase"], "restarting-service")
            coordinator._rollback(self.profile, state, self.root / "coordinator-state.json")
        self.assertEqual(state["decision"], "rolledback")
        self.assertEqual(calls.count(("restart", "isolated.service")), 2)


class OperatorRecoveryCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.state_root = self.root / "state"
        directory(self.state_root)

    def config(self) -> Path:
        profile_root = self.root / "target"
        profile = {
            "platform": "darwin" if sys.platform == "darwin" else "linux",
            "home": str(profile_root / "home"),
            "uv": str(profile_root / "uv"),
            "base_python": str(profile_root / "python"),
            "tool_root": str(profile_root / "tool"),
            "config": str(profile_root / "config.json"),
            "unit": str(profile_root / "unit"),
            "state_root": str(profile_root / "state"),
            "rollback_root": str(profile_root / "rollbacks"),
            "omp": str(profile_root / "omp"),
            "entrypoint": str(profile_root / "entrypoint"),
            "plugin_expected": str(profile_root / "plugin"),
            "port": 19878,
        }
        path = self.root / "release-config.json"
        save(
            path,
            {
                "version": 1,
                "state_root": str(self.state_root),
                "targets": [
                    {
                        "id": "isolated-node",
                        "kind": "node",
                        "ssh": None,
                        "work_root": str(profile_root / "work"),
                        "bootstrap_rollback": str(profile_root / "rollback.whl"),
                        "origin": "http://127.0.0.1:19878",
                        "machine_ids": [],
                        "profile": profile,
                    }
                ],
            },
        )
        return path

    def abandoned(self) -> None:
        release_root = self.state_root / "old-release"
        directory(release_root)
        save(
            release_root / "release.json",
            {"version": 1, "transaction": "old-release", "state": "preparing", "targets": []},
        )
        save(
            self.state_root / "fleet.json",
            {
                "version": 1,
                "active": "old-release",
                "owner": {"pid": os.getpid(), "nonce": "stale-owner"},
                "targets": {},
            },
        )

    def claim_without_journal(self) -> None:
        directory(self.state_root / "old-release")
        save(
            self.state_root / "fleet.json",
            {
                "version": 1,
                "active": "old-release",
                "owner": {"pid": os.getpid(), "nonce": "stale-owner"},
                "targets": {},
            },
        )

    def test_actual_cli_resume_releases_prebuild_journal_with_reused_pid(self) -> None:
        config = self.config()
        self.abandoned()
        result = subprocess.run(
            [sys.executable, str(ROOT / "tools/release.py"), "--config", str(config), "resume", "old-release"],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["state"], "prepare_failed")
        next_release = Release(
            {"state_root": str(self.state_root), "targets": []},
            ROOT,
            "new-release",
        )
        with (
            patch("tools.release_lib.orchestrator.prepare_build", side_effect=RuntimeError("build reached")),
            self.assertRaisesRegex(RuntimeError, "build reached"),
        ):
            next_release.prepare()
        self.assertIsNone(load(self.state_root / "fleet.json")["active"])

        fleet = load(self.state_root / "fleet.json")
        self.assertIsNone(fleet["active"])
        self.assertIsNone(fleet["owner"])

    def test_actual_cli_resume_releases_empty_missing_journal_claim(self) -> None:
        config = self.config()
        self.claim_without_journal()
        result = subprocess.run(
            [sys.executable, str(ROOT / "tools/release.py"), "--config", str(config), "resume", "old-release"],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["state"], "prepare_failed")
        self.assertEqual(load(self.state_root / "old-release/release.json")["state"], "prepare_failed")
        fleet = load(self.state_root / "fleet.json")
        self.assertIsNone(fleet["active"])
        self.assertIsNone(fleet["owner"])

    def test_missing_journal_recovery_preserves_unknown_assets_and_claim(self) -> None:
        self.claim_without_journal()
        unknown = self.state_root / "old-release/unknown-asset"
        unknown.write_text("preserve")
        release = Release({"state_root": str(self.state_root), "targets": []}, ROOT, "old-release")
        with self.assertRaisesRegex(ReleaseError, "preserving unknown"):
            release.deploy(resume=True)
        self.assertEqual(unknown.read_text(), "preserve")
        self.assertEqual(load(self.state_root / "fleet.json")["active"], "old-release")

    def test_initial_journal_is_durable_before_fleet_claim(self) -> None:
        save(self.state_root / "fleet.json", {"version": 1, "active": None, "owner": None, "targets": {}})
        release = Release({"state_root": str(self.state_root), "targets": []}, ROOT, "new-release")
        with patch.object(release, "save", side_effect=OSError("simulated journal failure")):
            with self.assertRaisesRegex(OSError, "simulated"):
                release.prepare()
        fleet = load(self.state_root / "fleet.json")
        self.assertIsNone(fleet["active"])
        self.assertIsNone(fleet["owner"])

    def test_process_lifetime_lock_rejects_a_concurrent_operator(self) -> None:
        release = Release({"state_root": str(self.state_root), "targets": []}, ROOT, "one")
        code = (
            "import sys; from pathlib import Path; "
            "from tools.release_lib.release_lease import operator_lock; "
            "lock=operator_lock(Path(sys.argv[1])); lock.__enter__(); "
            "print('locked', flush=True); sys.stdin.read(); lock.__exit__(None, None, None)"
        )
        environment = {
            **os.environ,
            "PYTHONPATH": os.pathsep.join((str(ROOT), str(ROOT / "src"))),
        }
        process = subprocess.Popen(
            [sys.executable, "-c", code, str(self.state_root / "operator.lock")],
            cwd=ROOT,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual(process.stdout.readline().strip(), "locked")
            with self.assertRaisesRegex(ReleaseError, "still running"):
                with release.lease():
                    self.fail("concurrent lease unexpectedly acquired")
        finally:
            process.stdin.close()
            process.wait(timeout=10)
            process.stdout.close()
            process.stderr.close()


def _admission(value):
    class Admission:
        def __enter__(self):
            return value

        def __exit__(self, *_args):
            return False

    return Admission()


if __name__ == "__main__":
    unittest.main()
