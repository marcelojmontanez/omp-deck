import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from omp_deck import lifecycle
from omp_deck.installation_paths import installation_paths, manifest_content, mark_owned


class FakeRunner:
    def __init__(self, results=None):
        self.calls = []
        self.results = list(results or [])

    def __call__(self, command, *, check=True, capture_output=False):
        self.calls.append((command, check, capture_output))
        if self.results:
            return self.results.pop(0)
        return SimpleNamespace(returncode=0, stdout="")


class LifecycleTests(unittest.TestCase):
    def environment(self, home):
        return {
            "HOME": str(home),
            "XDG_DATA_HOME": str(home / "data"),
            "XDG_CONFIG_HOME": str(home / "config"),
            "XDG_STATE_HOME": str(home / "state"),
            "OMP_DECK_BIN_DIR": str(home / "bin"),
            "PI_CODING_AGENT_DIR": str(home / "omp-agent"),
        }

    def settings(self, state_root):
        return SimpleNamespace(
            state_root=state_root,
            host="127.0.0.1",
            port=8877,
            omp_bin="/usr/bin/omp",
        )

    def test_service_install_generates_unit_without_touching_production(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config = home / "config/omp deck/config.json"
            state = home / "state/omp deck"
            runner = FakeRunner()
            with patch.dict(os.environ, self.environment(home), clear=False):
                paths = lifecycle.install_service(
                    self.settings(state), config, runner=runner, start=False
                )

            unit = paths.unit_path.read_text()
            self.assertIn(f'Environment="OMP_DECK_CONFIG={config}"', unit)
            self.assertIn(f'ExecStart="{os.sys.executable}" -m omp_deck serve', unit)
            self.assertNotIn("WorkingDirectory", unit)
            self.assertEqual(
                [call[0] for call in runner.calls],
                [
                    ["systemctl", "--user", "daemon-reload"],
                    ["systemctl", "--user", "enable", "omp-deck.service"],
                ],
            )
            self.assertEqual(paths.manifest_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(paths.manifest_path.read_text())["state_root"], str(state))
            self.assertEqual(state.stat().st_mode & 0o777, 0o700)
            self.assertEqual(paths.config_root.stat().st_mode & 0o777, 0o700)
            self.assertEqual(paths.config_path.stat().st_mode & 0o777, 0o600)

    def test_running_service_is_restarted_after_install(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            runner = FakeRunner()
            with patch.dict(os.environ, self.environment(home), clear=False):
                lifecycle.install_service(
                    self.settings(home / "runtime-state"),
                    home / "config/omp-deck/config.json",
                    runner=runner,
                )

            self.assertEqual(
                [call[0] for call in runner.calls],
                [
                    ["systemctl", "--user", "daemon-reload"],
                    ["systemctl", "--user", "enable", "omp-deck.service"],
                    ["systemctl", "--user", "restart", "omp-deck.service"],
                ],
            )

    def test_install_refuses_unrelated_state_root_without_mutating_it(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            state = home / "unrelated-state"
            state.mkdir()
            sentinel = state / "keep.txt"
            sentinel.write_text("preserved\n")
            state.chmod(0o755)
            with patch.dict(os.environ, self.environment(home), clear=False):
                with self.assertRaisesRegex(RuntimeError, "non-empty unowned path"):
                    lifecycle.install_service(
                        self.settings(state),
                        home / "config/omp-deck/config.json",
                        runner=FakeRunner(),
                        start=False,
                    )

            self.assertEqual(state.stat().st_mode & 0o777, 0o755)
            self.assertEqual(sentinel.read_text(), "preserved\n")
            self.assertFalse((state / ".omp-deck-owned").exists())


    def test_fresh_setup_can_claim_state_after_auth_initialization(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            state = home / "state/omp-deck"
            state.mkdir(parents=True)
            auth = state / "auth.json"
            auth.write_text('{"version":1}')
            auth.chmod(0o600)
            lock = state / ".auth.lock"
            lock.write_text("")
            lock.chmod(0o600)
            with patch.dict(os.environ, self.environment(home), clear=False):
                paths = lifecycle.install_service(
                    self.settings(state),
                    home / "config/omp-deck/config.json",
                    runner=FakeRunner(),
                    start=False,
                )

            self.assertTrue((state / ".omp-deck-owned").is_file())
            self.assertTrue(paths.manifest_path.is_file())
            self.assertEqual(state.stat().st_mode & 0o777, 0o700)

    def test_service_remove_uses_manifest_without_loading_runtime_config(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            environment = self.environment(home)
            with patch.dict(os.environ, environment, clear=False):
                paths = installation_paths()
                mark_owned(paths.install_root)
                mark_owned(paths.config_root)
                mark_owned(paths.state_root)
                paths.manifest_path.write_text(manifest_content(paths))
                paths.unit_path.parent.mkdir(parents=True, exist_ok=True)
                paths.unit_path.write_text("owned unit")
                runner = FakeRunner()
                removed = lifecycle.remove_service(runner=runner)

            self.assertEqual(removed, paths)
            self.assertFalse(paths.unit_path.exists())
            self.assertEqual(
                runner.calls[0][0],
                ["systemctl", "--user", "disable", "--now", "omp-deck.service"],
            )

    def test_plugin_install_ignores_unrelated_doctor_warnings_and_uses_official_manager(self):
        runner = FakeRunner(
            [
                SimpleNamespace(returncode=0, stdout=""),
                SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps([
                        {"name": "package_manifest", "status": "warning"},
                        {"name": "plugin:omp-deck", "status": "ok"},
                    ]),
                ),
                SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "npm": [
                                {
                                    "name": "omp-deck",
                                    "version": "0.1.0",
                                    "manifest": {"version": "0.1.0"},
                                }
                            ]
                        }
                    ),
                ),
                SimpleNamespace(returncode=0, stdout=""),
            ]
        )
        settings = self.settings(Path("/tmp/state"))

        lifecycle.install_plugin(settings, runner=runner)
        lifecycle.remove_plugin(settings.omp_bin, runner=runner)

        self.assertEqual(
            runner.calls[0][0],
            ["/usr/bin/omp", "plugin", "install", str(lifecycle.PLUGIN_ROOT)],
        )
        self.assertEqual(
            runner.calls[1][0],
            ["/usr/bin/omp", "plugin", "doctor", "--json"],
        )
        self.assertEqual(
            runner.calls[2][0],
            ["/usr/bin/omp", "plugin", "list", "--json"],
        )
        self.assertEqual(
            runner.calls[3][0],
            ["/usr/bin/omp", "plugin", "uninstall", "omp-deck"],
        )

    def test_systemd_values_reject_control_characters(self):
        with self.assertRaisesRegex(ValueError, "control"):
            lifecycle.service_unit("/tmp/config\nExecStart=/bin/false")

    def test_missing_manifest_prevents_removing_unrelated_unit(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            with patch.dict(os.environ, self.environment(home), clear=False):
                paths = installation_paths()
                paths.unit_path.parent.mkdir(parents=True)
                paths.unit_path.write_text("unrelated")
                with self.assertRaisesRegex(RuntimeError, "manifest is missing"):
                    lifecycle.remove_service(runner=FakeRunner())

            self.assertEqual(paths.unit_path.read_text(), "unrelated")

    def test_service_install_rejects_invalid_existing_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            with patch.dict(os.environ, self.environment(home), clear=False):
                paths = installation_paths()
                paths.unit_path.parent.mkdir(parents=True)
                paths.install_root.mkdir(parents=True)
                paths.unit_path.write_text("unrelated")
                paths.manifest_path.write_text("{}")
                with self.assertRaisesRegex(RuntimeError, "invalid"):
                    lifecycle.install_service(
                        self.settings(home / "state"),
                        home / "config/omp-deck/config.json",
                        runner=FakeRunner(),
                    )

            self.assertEqual(paths.unit_path.read_text(), "unrelated")

    def test_service_snapshot_restores_files_before_restarting_previous_service(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config = home / "config/omp-deck/config.json"
            state = home / "state/omp-deck"
            settings = self.settings(state)
            with patch.dict(os.environ, self.environment(home), clear=False):
                paths = installation_paths(config_file=config, state_root=state)
                paths.unit_path.parent.mkdir(parents=True)
                paths.install_root.mkdir(parents=True)
                paths.config_root.mkdir(parents=True)
                paths.unit_path.write_text("old unit\n")
                paths.manifest_path.write_text("old manifest\n")
                paths.config_path.write_text("old config\n")
                capture_runner = FakeRunner([
                    SimpleNamespace(returncode=1, stdout="disabled\n"),
                    SimpleNamespace(returncode=0, stdout="active\n"),
                ])
                snapshot = lifecycle.capture_service_state(
                    settings=settings,
                    config_path=config,
                    runner=capture_runner,
                )
                paths.unit_path.write_text("new unit\n")
                paths.manifest_path.write_text("new manifest\n")
                paths.config_path.write_text("new config\n")
                observations = []

                def runner(command, *, check=True, capture_output=False):
                    observations.append((command, paths.config_path.read_text()))
                    return SimpleNamespace(returncode=0, stdout="")

                lifecycle.restore_service_state(snapshot, runner=runner)

            self.assertEqual(paths.unit_path.read_text(), "old unit\n")
            self.assertEqual(paths.manifest_path.read_text(), "old manifest\n")
            self.assertEqual(paths.config_path.read_text(), "old config\n")
            self.assertEqual(
                [command for command, _ in observations],
                [
                    ["systemctl", "--user", "stop", "omp-deck.service"],
                    ["systemctl", "--user", "daemon-reload"],
                    ["systemctl", "--user", "disable", "omp-deck.service"],
                    ["systemctl", "--user", "restart", "omp-deck.service"],
                ],
            )
            self.assertEqual(observations[0][1], "new config\n")
            self.assertTrue(
                all(content == "old config\n" for _, content in observations[1:])
            )
    def test_health_check_retries_startup_race_until_service_is_ready(self):
        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            @staticmethod
            def read():
                return b'{"ok":true,"configuration":true,"authentication":true}'

        with patch.object(
            lifecycle.urllib.request,
            "urlopen",
            side_effect=[lifecycle.urllib.error.URLError("starting"), Response()],
        ) as urlopen:
            payload = lifecycle.check_health(self.settings(Path("/tmp/state")), timeout=1)

        self.assertTrue(payload["ok"])
        self.assertEqual(urlopen.call_count, 2)

    def test_fresh_installation_rollback_removes_every_created_owned_root(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            state = home / "state/omp-deck"
            config = home / "config/omp-deck/config.json"
            settings = self.settings(state)
            capture_runner = FakeRunner(
                [
                    SimpleNamespace(returncode=1, stdout="disabled\n"),
                    SimpleNamespace(returncode=1, stdout="inactive\n"),
                ]
            )
            with patch.dict(os.environ, self.environment(home), clear=False):
                snapshot = lifecycle.capture_service_state(
                    settings=settings,
                    config_path=config,
                    runner=capture_runner,
                )
                paths = lifecycle.install_service(
                    settings, config, runner=FakeRunner(), start=False
                )
                (state / "auth.json").write_text("generated", encoding="utf-8")
                lifecycle.restore_service_state(snapshot, runner=FakeRunner())

            self.assertFalse(paths.unit_path.exists())
            self.assertFalse(paths.install_root.exists())
            self.assertFalse(paths.config_root.exists())
            self.assertFalse(paths.state_root.exists())

    def test_rollback_removes_generated_entries_from_newly_claimed_state(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            state = home / "state/omp-deck"
            state.mkdir(parents=True)
            (state / "auth.json").write_text("existing")
            (state / ".auth.lock").write_text("")
            state.chmod(0o755)
            config = home / "config/omp-deck/config.json"
            settings = self.settings(state)
            capture_runner = FakeRunner([
                SimpleNamespace(returncode=1, stdout="disabled\n"),
                SimpleNamespace(returncode=1, stdout="inactive\n"),
            ])
            with patch.dict(os.environ, self.environment(home), clear=False):
                snapshot = lifecycle.capture_service_state(
                    settings=settings,
                    config_path=config,
                    runner=capture_runner,
                )
                lifecycle.install_service(
                    settings,
                    config,
                    runner=FakeRunner(),
                    start=False,
                )
                (state / "registry.json").write_text("{}")
                (state / "requests.json").write_text("{}")
                lifecycle.restore_service_state(snapshot, runner=FakeRunner())

            self.assertEqual(
                sorted(path.name for path in state.iterdir()),
                [".auth.lock", "auth.json"],
            )
            self.assertEqual(state.stat().st_mode & 0o777, 0o755)
    def test_plugin_backup_is_restorable_through_official_manager(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plugin = root / "installed-plugin"
            plugin.mkdir()
            (plugin / "package.json").write_text('{"name":"omp-deck","version":"0.0.9"}')
            (plugin / "omp-deck.ts").write_text("export default function old() {}\n")
            settings = self.settings(root / "state" / "omp-deck")
            config = root / "config" / "omp-deck" / "config.json"
            runner = FakeRunner([
                SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({
                        "npm": [{
                            "name": "omp-deck",
                            "path": str(plugin),
                        }]
                    }),
                )
            ])

            with patch.dict(os.environ, self.environment(root), clear=False):
                snapshot = lifecycle.capture_service_state(
                    settings=settings,
                    config_path=config,
                    runner=FakeRunner(),
                )
                lifecycle.install_service(
                    settings,
                    config,
                    runner=FakeRunner(),
                    start=False,
                )
                backup = lifecycle.backup_plugin(settings, runner=runner)
                lifecycle.restore_plugin(settings, backup, runner=runner)
                lifecycle.restore_service_state(snapshot, runner=FakeRunner())

            self.assertTrue(backup.exists())
            self.assertEqual(backup.parent.parent, settings.state_root.parent)
            self.assertEqual((backup / "omp-deck.ts").read_text(), "export default function old() {}\n")
            self.assertEqual(
                runner.calls[-1][0],
                ["/usr/bin/omp", "plugin", "install", str(backup)],
            )

    def test_plugin_backup_reuses_existing_rollback_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_root = root / "state" / "omp-deck"
            backup = (
                state_root.parent
                / ".omp-deck-setup-rollback-existing"
                / "plugin"
            )
            backup.mkdir(parents=True)
            source = backup / "omp-deck.ts"
            source.write_text("preserved\n")
            settings = SimpleNamespace(
                omp_bin="/usr/bin/omp",
                state_root=state_root,
            )
            runner = FakeRunner([
                SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({
                        "npm": [{"name": "omp-deck", "path": str(backup)}]
                    }),
                )
            ])

            result = lifecycle.backup_plugin(settings, runner=runner)

            self.assertEqual(result, backup)
            self.assertEqual(source.read_text(), "preserved\n")


if __name__ == "__main__":
    unittest.main()
