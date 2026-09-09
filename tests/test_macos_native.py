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
