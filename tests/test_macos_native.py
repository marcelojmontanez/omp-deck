from __future__ import annotations

import json
import os
import plistlib
import socket
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from omp_deck import _macos_lifecycle, lifecycle
from omp_deck.auth import AuthManager
from omp_deck.settings import Settings

_DARWIN = sys.platform == "darwin"


class MacLifecycleDefinitionTests(unittest.TestCase):
    def settings(self, root: Path):
        return SimpleNamespace(
            state_root=root / "state",
            omp_bin=str(root / ".local/bin/omp"),
        )

    def test_launch_agent_uses_absolute_arguments_and_bounded_restart_policy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            config = root / "config with spaces/omp-deck/config.json"
            environment = {
                "HOME": str(root),
                "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_STATE_HOME": str(root / "state-home"),
                "PI_CODING_AGENT_DIR": str(root / "omp-profile"),
                "OMP_PROFILE": "work/profile",
                "PI_PROFILE": "legacy profile",
            }
            with patch.dict(os.environ, environment, clear=False):
                definition = plistlib.loads(
                    _macos_lifecycle.launch_agent_plist(
                        config,
                        self.settings(root),
                        executable=str(root / "venv with spaces/bin/python"),
                    )
                )

        self.assertEqual(definition["Label"], "com.omp-deck.agent")
        self.assertEqual(
            definition["ProgramArguments"],
            [
                str(root / "venv with spaces/bin/python"),
                "-m",
                "omp_deck",
                "serve",
                "--config",
                str(config),
            ],
        )
        service_environment = definition["EnvironmentVariables"]
        self.assertEqual(service_environment["OMP_PROFILE"], "work/profile")
        self.assertEqual(service_environment["PI_PROFILE"], "legacy profile")
        self.assertEqual(
            service_environment["PI_CODING_AGENT_DIR"],
            str(root / "omp-profile"),
        )
        self.assertEqual(definition["KeepAlive"], {"SuccessfulExit": False})
        self.assertTrue(definition["RunAtLoad"])
        self.assertNotIn("StandardOutPath", definition)
        self.assertNotIn("StandardErrorPath", definition)

    def test_disabled_state_accepts_current_and_historic_launchctl_values(self):
        def runner(stdout):
            return lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=stdout)

        self.assertTrue(
            _macos_lifecycle._disabled(runner('disabled services = {\n  "com.omp-deck.agent" => disabled\n}\n'))
        )
        self.assertFalse(
            _macos_lifecycle._disabled(runner('disabled services = {\n  "com.omp-deck.agent" => false\n}\n'))
        )
        with self.assertRaisesRegex(RuntimeError, "unrecognized"):
            _macos_lifecycle._disabled(runner('disabled services = {\n  "com.omp-deck.agent" => maybe\n}\n'))
        with self.assertRaisesRegex(RuntimeError, "unrecognized"):
            _macos_lifecycle._disabled(runner("unexpected report format"))

    def test_missing_gui_domain_fails_with_login_requirement(self):
        def runner(*_args, **_kwargs):
            return SimpleNamespace(returncode=113, stdout="")

        with self.assertRaisesRegex(RuntimeError, "logged-in graphical user session"):
            _macos_lifecycle.service_status(runner=runner)

    def test_relocation_rollback_validates_and_restarts_restored_plist(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old_config = root / "old/config.json"
            new_config = root / "new/config.json"
            unit = root / "Library/LaunchAgents/com.omp-deck.agent.plist"
            unit.parent.mkdir(parents=True)
            old_definition = _macos_lifecycle.launch_agent_plist(
                old_config,
                self.settings(root),
                executable="/usr/bin/python3",
            )
            unit.write_bytes(b"new")
            paths = SimpleNamespace(
                unit_path=unit,
                config_path=new_config,
                manifest_path=root / "manifest",
            )
            snapshot = lifecycle.ServiceSnapshot(
                unit=old_definition,
                unit_mode=0o644,
                manifest=None,
                manifest_mode=None,
                enabled=True,
                active=True,
                installation=paths,
                roots=(),
            )
            calls = []

            def runner(command, *, check=True, capture_output=False):
                calls.append(command)
                return SimpleNamespace(returncode=0, stdout="")

            with (
                patch.object(_macos_lifecycle, "_require_gui_domain", return_value="gui/1"),
                patch.object(_macos_lifecycle, "_loaded", return_value=False),
                patch.object(
                    _macos_lifecycle,
                    "_validate_existing_service",
                    return_value=paths,
                ),
            ):
                _macos_lifecycle.restore_service_state(snapshot, runner=runner)

            restored = plistlib.loads(unit.read_bytes())
            self.assertEqual(restored["ProgramArguments"][-1], str(old_config.absolute()))
            self.assertNotEqual(restored["ProgramArguments"][-1], str(new_config.absolute()))
            self.assertIn(
                ["launchctl", "bootstrap", "gui/1", str(unit)],
                calls,
            )

    def test_rollback_rejects_foreign_restored_plist_before_bootstrap(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            unit = root / "agent.plist"
            paths = SimpleNamespace(unit_path=unit, config_path=root / "config.json", manifest_path=root / "manifest")
            definition = plistlib.loads(
                _macos_lifecycle.launch_agent_plist(
                    paths.config_path, self.settings(root), executable="/usr/bin/python3"
                )
            )
            definition["Label"] = "org.unrelated.service"
            snapshot = lifecycle.ServiceSnapshot(
                unit=plistlib.dumps(definition),
                unit_mode=0o644,
                manifest=None,
                manifest_mode=None,
                enabled=True,
                active=True,
                installation=paths,
                roots=(),
            )
            calls = []

            def runner(command, *, check=True, capture_output=False):
                calls.append(command)
                return SimpleNamespace(returncode=0, stdout="")

            with (
                patch.object(_macos_lifecycle, "_require_gui_domain", return_value="gui/1"),
                patch.object(_macos_lifecycle, "_loaded", return_value=False),
                patch.object(_macos_lifecycle, "_validate_existing_service", return_value=paths),
            ):
                with self.assertRaises(RuntimeError):
                    _macos_lifecycle.restore_service_state(snapshot, runner=runner)

            self.assertFalse(any(command[:2] == ["launchctl", "bootstrap"] for command in calls))


@unittest.skipUnless(
    _DARWIN and os.environ.get("OMP_DECK_MACOS_LAUNCHD_SMOKE") == "1",
    "set OMP_DECK_MACOS_LAUNCHD_SMOKE=1 for LaunchAgent integration",
)
class MacLaunchdIntegrationTests(unittest.TestCase):
    def test_launch_agent_install_restart_restore_and_remove(self):
        temporary = tempfile.TemporaryDirectory(prefix="omp-deck-launchd-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]

        label = f"com.omp-deck.agent.test.{uuid.uuid4().hex}"
        environment = {
            "HOME": str(root),
            "XDG_CACHE_HOME": str(root / "cache"),
            "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_DATA_HOME": str(root / "data"),
            "XDG_STATE_HOME": str(root / "state"),
            "PI_CODING_AGENT_DIR": str(root / "omp-profile"),
        }
        config = root / "config/omp-deck/config.json"
        state_root = root / "state/omp-deck"
        project_root = root / "projects"
        project_root.mkdir()
        settings = Settings(
            host="127.0.0.1",
            port=port,
            session_root=root / "sessions",
            socket_root=state_root / "sockets",
            state_root=state_root,
            project_root=project_root,
            project_discovery_depth=2,
            omp_bin=sys.executable,
            allowed_hosts=("127.0.0.1", "localhost"),
        )
        target = f"gui/{os.geteuid()}/{label}"
        plist_path = root / "Library/LaunchAgents/com.omp-deck.agent.plist"

        def cleanup():
            subprocess.run(
                ["launchctl", "bootout", target],
                check=False,
                capture_output=True,
                text=True,
            )
            plist_path.unlink(missing_ok=True)

        self.addCleanup(cleanup)
        with (
            patch.dict(os.environ, environment, clear=False),
            patch.object(_macos_lifecycle, "_LABEL", label),
        ):
            initial = lifecycle.capture_service_state(settings=settings, config_path=config)
            lifecycle.write_atomic(
                config,
                json.dumps(
                    {
                        "host": settings.host,
                        "port": port,
                        "state_root": str(state_root),
                        "project_root": str(project_root),
                        "session_root": str(settings.session_root),
                        "socket_root": str(settings.socket_root),
                        "omp_bin": sys.executable,
                        "allowed_hosts": list(settings.allowed_hosts),
                    }
                )
                + "\n",
                0o600,
            )
            AuthManager(state_root)

            lifecycle.install_service(settings, config)
            self.assertTrue(lifecycle.check_health(settings, timeout=15)["ok"])
            self.assertEqual(lifecycle.service_status(), 0)
            lifecycle.restart_service()
            self.assertTrue(lifecycle.check_health(settings, timeout=15)["ok"])

            installed = lifecycle.capture_service_state(settings=settings, config_path=config)
            lifecycle.remove_service()
            self.assertEqual(lifecycle.service_status(), 1)
            lifecycle.restore_service_state(installed)
            self.assertEqual(lifecycle.service_status(), 0)
            self.assertTrue(lifecycle.check_health(settings, timeout=15)["ok"])

            lifecycle.remove_service()
            self.assertEqual(lifecycle.service_status(), 1)
            lifecycle.restore_service_state(initial)
            self.assertFalse(plist_path.exists())
            self.assertFalse(initial.installation.install_root.exists())
