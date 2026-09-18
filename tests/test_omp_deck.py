import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from omp_deck import cli, lifecycle
from omp_deck._omp_version import installed_omp_version
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
                        "show_history": True,
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
            self.assertTrue(configured.show_history)

    def test_service_omp_environment_pins_the_resolved_executable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.json"
            config.write_text('{"omp_bin":"omp"}', encoding="utf-8")
            config.chmod(0o600)

            with (
                patch.dict("os.environ", {"OMP_DECK_OMP_BIN": str(root / "bin/omp")}),
                patch("omp_deck.settings.sys.platform", "linux"),
            ):
                configured = load_settings(config)

            self.assertEqual(configured.omp_bin, str(root / "bin/omp"))

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

    def test_show_history_defaults_false_and_rejects_non_boolean_values(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            config.write_text("{}")
            config.chmod(0o600)
            self.assertFalse(load_settings(config).show_history)

            for value in (0, 1, "true", None, []):
                with self.subTest(value=value):
                    config.write_text(json.dumps({"show_history": value}))
                    config.chmod(0o600)
                    with self.assertRaisesRegex(RuntimeError, "show_history"):
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

    def test_plugin_install_machine_report_excludes_native_diagnostics(self):
        script = """
from pathlib import Path
from unittest.mock import patch
from omp_deck import cli
from omp_deck._plugin_lifecycle import run
def install(*args, **kwargs):
    run([cli.sys.executable, "-c", "print('native plugin diagnostic')"])
    return Path("plugin-new")
with patch.object(cli, "load_settings"), patch.object(cli, "install_plugin", side_effect=install):
    raise SystemExit(cli.main(["plugin", "install", "--json"]))
"""
        result = subprocess.run(
            [cli.sys.executable, "-c", script],
            cwd=Path(cli.__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(json.loads(result.stdout), {"version": 1, "pluginSource": "plugin-new"})
        self.assertIn("native plugin diagnostic", result.stderr)

    def test_pair_refuses_tailscale_auth_mode_without_creating_state(self):
        with tempfile.TemporaryDirectory() as directory:
            state_root = Path(directory) / "state"
            settings = SimpleNamespace(
                auth_mode="tailscale",
                tailscale_user="owner@example.com",
                state_root=state_root,
            )
            with patch.object(cli, "load_settings", return_value=settings):
                with self.assertRaises(RuntimeError):
                    cli.pair()
            self.assertFalse((state_root / "auth.json").exists())

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

    def test_stock_omp_version_output_accepts_future_versions_from_authoritative_metadata(self):
        for output, expected in (
            ("omp/18.1.16\n", "18.1.16"),
            ("omp/99.42.7\n", "99.42.7"),
            ("omp/99.42.7-rc.1+build.abc\n", "99.42.7-rc.1+build.abc"),
            ("18.1.16\n", None),
            ("omp/not-a-version\n", None),
        ):
            with self.subTest(output=output):
                runner = Mock(return_value=SimpleNamespace(stdout=output, returncode=0))
                self.assertEqual(installed_omp_version("/opt/omp", runner=runner), expected)

    def test_failed_version_query_rejects_partial_output(self):
        for error in (
            FileNotFoundError(),
            subprocess.TimeoutExpired(["omp", "--version"], 2, output=b"omp/18.2.1\n"),
        ):
            with self.subTest(error=type(error).__name__):
                runner = Mock(side_effect=error)
                self.assertIsNone(installed_omp_version("omp", runner=runner))
        failed = Mock(return_value=SimpleNamespace(stdout="omp/18.2.1\n", returncode=1))
        self.assertIsNone(installed_omp_version("omp", runner=failed))

    def test_doctor_treats_native_versions_as_observations_separate_from_plugin_health(self):
        settings = SimpleNamespace(
            omp_bin="/opt/omp",
            socket_root=Path("/private/sockets"),
            state_root=Path("/private/state"),
            host="127.0.0.1",
            port=8787,
        )
        cases = (
            ("99.42.7", True, "OK", "OK"),
            (None, False, "OPTIONAL", "OPTIONAL"),
        )
        for version, running_healthy, installed_state, running_state in cases:
            output = io.StringIO()
            with (
                self.subTest(version=version),
                patch.object(cli, "load_settings", return_value=settings),
                patch.object(cli, "config_path", return_value=Path("/private/config.json")),
                patch.object(cli, "_health_diagnostic", return_value=(True, "healthy")),
                patch.object(cli.shutil, "which", return_value="/opt/omp"),
                patch.object(cli, "installed_omp_version", return_value=version),
                patch.object(
                    cli,
                    "_running_versions_diagnostic",
                    return_value=(running_healthy, "live metadata observation"),
                ),
                patch.object(cli, "service_status", return_value=0),
                patch.object(cli, "plugin_status", return_value=True),
                patch.object(cli, "_auth_healthy", return_value=True),
                redirect_stdout(output),
            ):
                self.assertEqual(cli.doctor(), 0)
                self.assertRegex(output.getvalue(), r"OK\s+Extension package")
                self.assertRegex(output.getvalue(), rf"{installed_state}\s+Installed OMP version")
                self.assertRegex(output.getvalue(), rf"{running_state}\s+Running OMP versions")

    def test_live_extension_versions_are_reported_without_connecting(self):
        settings = SimpleNamespace(socket_root=Path("/private/sockets"))
        candidates = {
            "session": ((Path("/private/sockets/live.sock"), {"ompVersion": "99.42.7"}),),
            "older": ((Path("/private/sockets/older.sock"), {"ompVersion": "18.1.16"}),),
        }
        with patch.object(cli, "discover_extension_candidates", return_value=candidates) as discover:
            healthy, detail = cli._running_versions_diagnostic(settings)

        self.assertTrue(healthy)
        self.assertIn("18.1.16", detail)
        self.assertIn("99.42.7", detail)
        discover.assert_called_once_with(settings.socket_root, all_candidates=True)

    def test_live_extension_version_diagnostic_rejects_unreadable_metadata(self):
        settings = SimpleNamespace(socket_root=Path("/private/sockets"))
        candidates = {
            "session": ((Path("/private/sockets/live.sock"), {"ompVersion": None}),),
        }
        with patch.object(cli, "discover_extension_candidates", return_value=candidates):
            healthy, detail = cli._running_versions_diagnostic(settings)

        self.assertFalse(healthy)
        self.assertTrue(detail)

    def test_setup_orders_plugin_service_health_and_pairing(self):
        settings = SimpleNamespace(
            omp_bin="omp",
            state_root=Path("/tmp/state"),
            auth_mode="pairing",
            tailscale_user=None,
        )
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
            patch.object(cli, "install_plugin", side_effect=lambda value, **_kwargs: calls.append("plugin")),
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
        settings = SimpleNamespace(
            omp_bin="omp",
            state_root=Path("/tmp/state"),
            auth_mode="pairing",
            tailscale_user=None,
        )
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
            settings = SimpleNamespace(
                omp_bin="/usr/bin/omp",
                state_root=state,
                auth_mode="pairing",
                tailscale_user=None,
            )
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
            settings = SimpleNamespace(
                omp_bin="omp",
                state_root=root / "state",
                auth_mode="pairing",
                tailscale_user=None,
            )
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
        settings = SimpleNamespace(
            omp_bin="omp",
            state_root=Path("/tmp/state"),
            auth_mode="pairing",
            tailscale_user=None,
        )
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
        settings = SimpleNamespace(
            omp_bin="omp",
            state_root=Path("/tmp/state"),
            auth_mode="pairing",
            tailscale_user=None,
        )
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
        settings = SimpleNamespace(
            omp_bin="omp",
            state_root=Path("/tmp/state"),
            auth_mode="pairing",
            tailscale_user=None,
        )
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

    def test_tailscale_cleanup_accepts_only_the_owned_https_serve_schema(self):
        configured = SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "TCP": {"443": {"HTTPS": True}},
                    "Web": {"deck.example.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8787"}}}},
                }
            ),
            stderr="",
        )
        reset = SimpleNamespace(returncode=0, stdout="", stderr="")
        with patch.object(cli.subprocess, "run", side_effect=[configured, reset]) as run:
            cli.disable_tailscale(SimpleNamespace(port=8787))

        self.assertEqual(run.call_args_list[-1].args[0], ["tailscale", "serve", "reset"])

    def test_tailscale_cleanup_refuses_unrelated_tcp_listener(self):
        configured = SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "TCP": {
                        "443": {"HTTPS": True},
                        "2222": {"TCPForward": "127.0.0.1:22"},
                    },
                    "Web": {"deck.example.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8787"}}}},
                }
            ),
            stderr="",
        )
        with patch.object(cli.subprocess, "run", return_value=configured) as run:
            with self.assertRaisesRegex(RuntimeError, "unrecognized"):
                cli.disable_tailscale(SimpleNamespace(port=8787))

        self.assertEqual(run.call_count, 1)

    def test_tailscale_setup_verifies_the_exact_proxy(self):
        empty = SimpleNamespace(returncode=0, stdout="{}", stderr="")
        configured = SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "TCP": {"443": {"HTTPS": True}},
                    "Web": {"deck.example.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8787"}}}},
                }
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
                {
                    "TCP": {"443": {"HTTPS": True}},
                    "Web": {"deck.example.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:9999"}}}},
                }
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
