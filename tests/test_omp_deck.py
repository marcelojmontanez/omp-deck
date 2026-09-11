import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from omp_deck import cli, lifecycle
from omp_deck.installation_paths import installation_paths
from omp_deck.settings import load_settings


class SettingsTests(unittest.TestCase):
    def test_config_overrides_port_project_root_and_allowed_hosts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.json"
            config.write_text(
                json.dumps(
                    {
                        "port": 9123,
                        "project_root": str(root / "code"),
                        "project_discovery_depth": 4,
                        "allowed_hosts": ["deck.example.ts.net"],
                    }
                )
            )
            config.chmod(0o600)

            configured = load_settings(config)

            self.assertEqual(configured.port, 9123)
            self.assertEqual(configured.project_root, root / "code")
            self.assertEqual(configured.project_discovery_depth, 4)
            self.assertIn("deck.example.ts.net", configured.allowed_hosts)
            self.assertEqual(configured.host, "127.0.0.1")

    def test_invalid_port_and_allowed_hosts_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            config.write_text('{"port": 70000}')
            config.chmod(0o600)
            with self.assertRaisesRegex(RuntimeError, "port"):
                load_settings(config)

            config.write_text('{"allowed_hosts": ["https://attacker.example"]}')
            config.chmod(0o600)
            with self.assertRaisesRegex(RuntimeError, "allowed_hosts"):
                load_settings(config)

    def test_invalid_project_discovery_depth_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            for value in (0, 9, True, "2"):
                with self.subTest(value=value):
                    config.write_text(json.dumps({"project_discovery_depth": value}))
                    config.chmod(0o600)
                    with self.assertRaisesRegex(RuntimeError, "project_discovery_depth"):
                        load_settings(config)

    def test_world_writable_configuration_is_rejected_before_values_are_used(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            config.write_text('{"omp_bin":"/tmp/attacker-controlled"}')
            config.chmod(0o666)

            with self.assertRaisesRegex(RuntimeError, "unsafe ownership or permissions"):
                load_settings(config)


class CommandTests(unittest.TestCase):
    def test_service_remove_does_not_load_runtime_configuration(self):
        with (
            patch.object(cli, "remove_service") as remove,
            patch.object(cli, "load_settings") as load_settings_mock,
        ):
            result = cli.main(["service", "remove"])

        self.assertEqual(result, 0)
        remove.assert_called_once_with()
        load_settings_mock.assert_not_called()

    def test_purge_uses_configured_omp_binary(self):
        settings = SimpleNamespace(omp_bin="/opt/omp/bin/omp")
        with (
            patch.object(cli, "load_settings", return_value=settings),
            patch.object(cli, "purge_installation") as purge,
        ):
            self.assertEqual(cli.main(["purge"]), 0)
        purge.assert_called_once_with("/opt/omp/bin/omp")

    def test_purge_falls_back_to_path_when_configuration_is_malformed(self):
        with (
            patch.object(cli, "load_settings", side_effect=RuntimeError("malformed")),
            patch.object(cli, "purge_installation") as purge,
        ):
            self.assertEqual(cli.main(["purge"]), 0)
        purge.assert_called_once_with("omp")

    def test_setup_orders_plugin_service_health_and_pairing(self):
        settings = SimpleNamespace(omp_bin="omp", state_root=Path("/tmp/state"))
        calls = []
        secret = SimpleNamespace(token="secret", expires_at=1)
        manager = SimpleNamespace(begin_pairing=lambda: secret)
        service_snapshot = object()
        with (
            patch.object(cli, "load_settings", return_value=settings),
            patch.object(cli, "config_path", return_value=Path("/tmp/config.json")),
            patch.object(cli.shutil, "which", return_value="/usr/bin/tool"),
            patch.object(cli, "preflight_service_install"),
            patch.object(cli, "plugin_installed", return_value=False),
            patch.object(cli, "capture_service_state", return_value=service_snapshot),
            patch.object(cli, "backup_plugin", return_value=None),
            patch.object(cli, "discard_plugin_backup", side_effect=lambda value: calls.append("discard")),
            patch.object(cli, "AuthManager", return_value=manager),
            patch.object(cli, "install_plugin", side_effect=lambda value: calls.append("plugin")),
            patch.object(
                cli,
                "install_service",
                side_effect=lambda value, path: calls.append(("service", path)),
            ),
            patch.object(cli, "check_health", side_effect=lambda value: calls.append("health")),
            patch.object(
                cli,
                "print_pairing",
                side_effect=lambda value: calls.append(("pair", value)),
            ),
        ):
            cli.setup()

        self.assertEqual(
            calls,
            [
                ("service", Path("/tmp/config.json")),
                "health",
                "plugin",
                "health",
                "discard",
                ("pair", secret),
            ],
        )

    def test_setup_rolls_back_new_plugin_and_service_after_failed_health(self):
        settings = SimpleNamespace(omp_bin="omp", state_root=Path("/tmp/state"))
        manager = SimpleNamespace(begin_pairing=lambda: SimpleNamespace(token="secret", expires_at=1))
        service_snapshot = object()
        with (
            patch.object(cli, "load_settings", return_value=settings),
            patch.object(cli, "config_path", return_value=Path("/tmp/config.json")),
            patch.object(cli.shutil, "which", return_value="/usr/bin/tool"),
            patch.object(cli, "preflight_service_install"),
            patch.object(cli, "plugin_installed", return_value=False),
            patch.object(cli, "capture_service_state", return_value=service_snapshot),
            patch.object(cli, "AuthManager", return_value=manager),
            patch.object(cli, "install_plugin") as install_plugin,
            patch.object(cli, "install_service"),
            patch.object(cli, "check_health", side_effect=RuntimeError("unhealthy")),
            patch.object(cli, "restore_service_state") as restore_service,
            patch.object(cli, "restore_plugin") as restore_plugin,
        ):
            with self.assertRaisesRegex(RuntimeError, "unhealthy"):
                cli.setup()

        restore_service.assert_called_once_with(service_snapshot)
        restore_plugin.assert_not_called()
        install_plugin.assert_not_called()

    def test_setup_rejects_invalid_existing_installation_without_stopping_or_mutating_it(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            environment = {
                "HOME": str(home),
                "XDG_DATA_HOME": str(home / "data"),
                "XDG_CONFIG_HOME": str(home / "config"),
                "XDG_STATE_HOME": str(home / "state"),
                "OMP_DECK_BIN_DIR": str(home / "bin"),
            }
            config = home / "config/omp-deck/config.json"
            state = home / "foreign-state"
            state.mkdir()
            sentinel = state / "keep.txt"
            sentinel.write_text("foreign\n")
            settings = SimpleNamespace(omp_bin="/usr/bin/omp", state_root=state)
            with patch.dict(cli.os.environ, environment, clear=False):
                paths = installation_paths(config_file=config, state_root=state)
                paths.unit_path.parent.mkdir(parents=True)
                paths.install_root.mkdir(parents=True)
                paths.unit_path.write_text("foreign service\n")
                paths.manifest_path.write_text("{}")
                commands = []

                def subprocess_boundary(command, **_kwargs):
                    commands.append(command)
                    if command[-3:] == ["plugin", "list", "--json"]:
                        return SimpleNamespace(returncode=0, stdout=json.dumps({"npm": []}))
                    if command[:3] == ["systemctl", "--user", "is-enabled"]:
                        return SimpleNamespace(returncode=0, stdout="enabled\n")
                    if command[:3] == ["systemctl", "--user", "is-active"]:
                        return SimpleNamespace(returncode=0, stdout="active\n")
                    return SimpleNamespace(returncode=0, stdout="")

                with (
                    patch.object(cli, "load_settings", return_value=settings),
                    patch.object(cli, "config_path", return_value=config),
                    patch.object(cli.shutil, "which", return_value="/usr/bin/tool"),
                    patch.object(lifecycle.subprocess, "run", side_effect=subprocess_boundary),
                ):
                    with self.assertRaisesRegex(RuntimeError, "invalid"):
                        cli.setup()

            self.assertNotIn(
                ["systemctl", "--user", "stop", "omp-deck.service"],
                commands,
            )
            self.assertEqual(paths.unit_path.read_text(), "foreign service\n")
            self.assertEqual(paths.manifest_path.read_text(), "{}")
            self.assertEqual(sentinel.read_text(), "foreign\n")

    def test_setup_keeps_restored_plugin_backup_after_late_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = SimpleNamespace(omp_bin="omp", state_root=root / "state")
            manager = SimpleNamespace(begin_pairing=lambda: SimpleNamespace(token="secret", expires_at=1))
            service_snapshot = object()
            plugin_backup = settings.state_root / "setup-rollback/plugin"
            plugin_backup.mkdir(parents=True)
            (plugin_backup / "omp-deck.ts").write_text("restored\n")
            installed_plugin = root / "omp-plugin"

            def restore_plugin(_settings, backup):
                installed_plugin.symlink_to(backup, target_is_directory=True)

            with (
                patch.object(cli, "load_settings", return_value=settings),
                patch.object(cli, "config_path", return_value=root / "config.json"),
                patch.object(cli.shutil, "which", return_value="/usr/bin/tool"),
                patch.object(cli, "preflight_service_install"),
                patch.object(cli, "plugin_installed", return_value=True),
                patch.object(cli, "capture_service_state", return_value=service_snapshot),
                patch.object(cli, "backup_plugin", return_value=plugin_backup),
                patch.object(cli, "AuthManager", return_value=manager),
                patch.object(cli, "install_plugin"),
                patch.object(cli, "install_service"),
                patch.object(cli, "check_health", side_effect=[None, RuntimeError("late failure")]),
                patch.object(cli, "restore_service_state") as restore_service,
                patch.object(cli, "restore_plugin", side_effect=restore_plugin),
                patch.object(cli, "discard_plugin_backup", wraps=cli.discard_plugin_backup) as discard_plugin_backup,
            ):
                with self.assertRaisesRegex(RuntimeError, "late failure"):
                    cli.setup()

            restore_service.assert_called_once_with(service_snapshot)
            discard_plugin_backup.assert_not_called()
            self.assertEqual((installed_plugin / "omp-deck.ts").read_text(), "restored\n")

    def test_setup_refuses_to_replace_unsnapshottable_existing_plugin(self):
        settings = SimpleNamespace(omp_bin="omp", state_root=Path("/tmp/state"))
        manager = SimpleNamespace(begin_pairing=lambda: SimpleNamespace(token="secret", expires_at=1))
        with (
            patch.object(cli, "load_settings", return_value=settings),
            patch.object(cli, "config_path", return_value=Path("/tmp/config.json")),
            patch.object(cli.shutil, "which", return_value="/usr/bin/tool"),
            patch.object(cli, "preflight_service_install"),
            patch.object(cli, "plugin_installed", return_value=True),
            patch.object(cli, "capture_service_state", return_value=object()),
            patch.object(cli, "backup_plugin", return_value=None),
            patch.object(cli, "AuthManager", return_value=manager),
            patch.object(cli, "install_service"),
            patch.object(cli, "check_health"),
            patch.object(cli, "install_plugin") as install_plugin,
            patch.object(cli, "restore_service_state"),
        ):
            with self.assertRaisesRegex(RuntimeError, "cannot snapshot"):
                cli.setup()

        install_plugin.assert_not_called()

    def test_setup_does_not_create_pairing_secret_before_success(self):
        settings = SimpleNamespace(omp_bin="omp", state_root=Path("/tmp/state"))
        manager = Mock()
        with (
            patch.object(cli, "load_settings", return_value=settings),
            patch.object(cli, "config_path", return_value=Path("/tmp/config.json")),
            patch.object(cli.shutil, "which", return_value="/usr/bin/tool"),
            patch.object(cli, "preflight_service_install"),
            patch.object(cli, "plugin_installed", return_value=False),
            patch.object(cli, "capture_service_state", return_value=object()),
            patch.object(cli, "AuthManager", return_value=manager),
            patch.object(cli, "install_service"),
            patch.object(cli, "check_health", side_effect=RuntimeError("unhealthy")),
            patch.object(cli, "restore_service_state"),
        ):
            with self.assertRaisesRegex(RuntimeError, "unhealthy"):
                cli.setup()
        manager.begin_pairing.assert_not_called()

    def test_setup_removes_tailscale_route_when_pairing_fails(self):
        settings = SimpleNamespace(omp_bin="omp", state_root=Path("/tmp/state"))
        manager = Mock()
        manager.begin_pairing.side_effect = RuntimeError("pairing failed")
        with (
            patch.object(cli, "load_settings", return_value=settings),
            patch.object(cli, "config_path", return_value=Path("/tmp/config.json")),
            patch.object(cli.shutil, "which", return_value="/usr/bin/tool"),
            patch.object(cli, "preflight_service_install"),
            patch.object(cli, "plugin_installed", return_value=False),
            patch.object(cli, "capture_service_state", return_value=object()),
            patch.object(cli, "backup_plugin", return_value=None),
            patch.object(cli, "AuthManager", return_value=manager),
            patch.object(cli, "install_service"),
            patch.object(cli, "install_plugin"),
            patch.object(cli, "check_health"),
            patch.object(cli, "enable_tailscale", return_value=True),
            patch.object(cli, "disable_tailscale") as disable,
            patch.object(cli, "restore_plugin"),
            patch.object(cli, "restore_service_state"),
        ):
            with self.assertRaisesRegex(RuntimeError, "pairing failed"):
                cli.setup(tailscale=True)
        disable.assert_called_once_with(settings)

    def test_tailscale_refuses_to_overwrite_existing_routes(self):
        existing = SimpleNamespace(returncode=0, stdout='{"Web": {"443": {}}}', stderr="")
        with (
            patch.object(cli.shutil, "which", return_value="/usr/bin/tailscale"),
            patch.object(cli.subprocess, "run", return_value=existing) as run,
        ):
            with self.assertRaisesRegex(RuntimeError, "left unchanged"):
                cli.enable_tailscale(SimpleNamespace(port=8787))

        run.assert_called_once_with(
            ["tailscale", "serve", "status", "--json"],
            capture_output=True,
            text=True,
        )

    def test_tailscale_setup_verifies_the_exact_proxy(self):
        empty = SimpleNamespace(returncode=0, stdout="{}", stderr="")
        configured = SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {"Web": {"deck.example.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8787"}}}}}
            ),
            stderr="",
        )
        command = SimpleNamespace(returncode=0, stdout="", stderr="")
        with (
            patch.object(cli.shutil, "which", return_value="/usr/bin/tailscale"),
            patch.object(cli.subprocess, "run", side_effect=[empty, command, configured]) as run,
        ):
            cli.enable_tailscale(SimpleNamespace(port=8787))

        self.assertEqual(run.call_count, 3)

    def test_tailscale_verification_failure_resets_new_route(self):
        empty = SimpleNamespace(returncode=0, stdout="{}", stderr="")
        command = SimpleNamespace(returncode=0, stdout="", stderr="")
        mismatched = SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {"Web": {"deck.example.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:9999"}}}}}
            ),
            stderr="",
        )
        with (
            patch.object(cli.shutil, "which", return_value="/usr/bin/tailscale"),
            patch.object(
                cli.subprocess,
                "run",
                side_effect=[empty, command, mismatched, command],
            ) as run,
        ):
            with self.assertRaisesRegex(RuntimeError, "did not expose"):
                cli.enable_tailscale(SimpleNamespace(port=8787))
        self.assertEqual(run.call_args_list[-1].args[0], ["tailscale", "serve", "reset"])

    def test_tailscale_cleanup_refuses_concurrent_routes(self):
        configured = SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "Web": {
                        "deck.example.ts.net:443": {
                            "Handlers": {
                                "/": {"Proxy": "http://127.0.0.1:8787"},
                                "/other": {"Proxy": "http://127.0.0.1:9999"},
                            }
                        }
                    }
                }
            ),
            stderr="",
        )
        with patch.object(cli.subprocess, "run", return_value=configured) as run:
            with self.assertRaisesRegex(RuntimeError, "unrecognized"):
                cli.disable_tailscale(SimpleNamespace(port=8787))
        self.assertEqual(run.call_count, 1)

    def test_config_validate_loads_only_the_requested_configuration(self):
        settings = SimpleNamespace(
            host="127.0.0.1",
            port=8787,
            project_root=Path("/tmp/projects"),
        )
        with (
            patch.object(cli, "config_path", return_value=Path("/tmp/config.json")),
            patch.object(cli, "load_settings", return_value=settings) as load,
        ):
            self.assertEqual(cli.main(["config", "validate"]), 0)

        load.assert_called_once_with(Path("/tmp/config.json"))


if __name__ == "__main__":
    unittest.main()
