from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools.release_lib import coordinator, coordinator_prepare


@unittest.skipIf(os.name == "nt", "coordinator release guard targets Linux services")
class CoordinatorReleaseTests(unittest.TestCase):
    def test_health_waits_for_service_readiness_after_restart(self) -> None:
        statuses = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                status = 503 if not statuses else 200
                statuses.append(status)
                self.send_response(status)
                self.end_headers()
                self.wfile.write(b'{"ok":true,"configuration":true,"authentication":true}')

            def log_message(self, *_args) -> None:
                pass

        with HTTPServer(("127.0.0.1", 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with mock.patch.object(
                    coordinator, "_native_profile", return_value=SimpleNamespace(port=server.server_port)
                ):
                    coordinator._health({"port": server.server_port})
                self.assertEqual(statuses, [503, 200])
            finally:
                server.shutdown()
                thread.join(timeout=2)

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

    @unittest.skipUnless(shutil.which("uv"), "uv is required for the coordinator environment regression")
    def test_prepare_with_real_uv_does_not_change_the_live_unit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            transaction = root / "transaction"
            transaction.mkdir(mode=0o700)
            (transaction / "guard").mkdir(mode=0o700)
            state_root = root / "state"
            state_root.mkdir(mode=0o700)
            profile = {
                "home": root,
                "uv": Path(shutil.which("uv")),
                "base_python": Path(sys.executable),
                "tool_root": root / "old",
                "releases_root": root / "releases",
                "unit": root / "coordinator.service",
                "config": root / "config.json",
                "state_root": state_root,
            }
            command = [
                str(profile["tool_root"] / "bin/python"),
                "-m",
                "omp_deck",
                "coordinator",
                "serve",
                "--config",
                str(profile["config"]),
            ]
            unit = (
                "[Service]\nExecStart=" + " ".join(coordinator_prepare.systemd_quote(value) for value in command) + "\n"
            ).encode()
            profile["unit"].write_bytes(unit)
            for path in (profile["config"], state_root / "fleet-coordinator.json"):
                path.write_text("{}")
                path.chmod(0o600)
            candidate = {"wheelSha256": "a" * 64, "wheelVersion": "2.0", "revision": "a" * 40}
            rollback = {"wheelSha256": "b" * 64, "wheelVersion": "1.0", "revision": "b" * 40}
            original_run = coordinator._run

            def run(argv, current_profile, timeout=300):
                if argv[1] == "venv":
                    return original_run(argv, current_profile, timeout)
                if argv[1:3] in (["pip", "freeze"], ["pip", "install"]):
                    return b""
                raise AssertionError("unexpected release command")

            with (
                mock.patch.object(
                    coordinator,
                    "_wheel",
                    side_effect=[
                        (candidate, root / "candidate.whl"),
                        (rollback, root / "rollback.whl"),
                    ],
                ),
                mock.patch.object(coordinator, "_active", return_value=True),
                mock.patch.object(coordinator, "_health"),
                mock.patch.object(
                    coordinator,
                    "_live_identity",
                    return_value={
                        "version": rollback["wheelVersion"],
                        "revision": rollback["revision"],
                    },
                ),
                mock.patch.object(
                    coordinator,
                    "_identity",
                    return_value={
                        "version": candidate["wheelVersion"],
                        "revision": candidate["revision"],
                    },
                ),
                mock.patch.object(coordinator, "_enabled", return_value=True),
                mock.patch.object(coordinator, "_run", side_effect=run),
                mock.patch.dict(
                    os.environ,
                    {
                        "XDG_CACHE_HOME": str(root / "cache"),
                        "XDG_CONFIG_HOME": str(root / "configuration"),
                        "UV_CACHE_DIR": str(root / "cache"),
                        "UV_OFFLINE": "1",
                    },
                ),
            ):
                result = coordinator_prepare.prepare(
                    {"transaction": "release-1", "candidate": candidate, "rollback": rollback},
                    profile,
                    transaction,
                    transaction / "guard/manifest.json",
                )
            self.assertEqual(result["state"], "prepared")
            self.assertEqual(profile["unit"].read_bytes(), unit)
            release = profile["releases_root"] / "release-1"
            prefix = original_run([release / "bin/python", "-c", "import sys; print(sys.prefix)"], profile, 30)
            self.assertEqual(prefix.decode().strip(), str(release))


if __name__ == "__main__":
    unittest.main()
