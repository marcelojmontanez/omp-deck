import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import omp_deck
from settings import load_settings


class SettingsTests(unittest.TestCase):
    def test_config_overrides_port_and_project_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.json"
            config.write_text(json.dumps({"port": 9123, "project_root": str(root / "code")}))

            configured = load_settings(config)

            self.assertEqual(configured.port, 9123)
            self.assertEqual(configured.project_root, root / "code")
            self.assertEqual(configured.host, "127.0.0.1")

    def test_invalid_port_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            config.write_text('{"port": 70000}')

            with self.assertRaisesRegex(RuntimeError, "port"):
                load_settings(config)


class InstallerTests(unittest.TestCase):
    def test_install_creates_service_command_and_private_config(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config = home / "config/omp-deck/config.json"
            settings = SimpleNamespace(omp_bin="/usr/bin/omp", host="127.0.0.1", port=8787)
            calls = []
            environment = {
                "XDG_DATA_HOME": str(home / "data"),
                "XDG_CONFIG_HOME": str(home / "config"),
                "OMP_DECK_BIN_DIR": str(home / "bin"),
                "PI_CODING_AGENT_DIR": str(home / "omp-agent"),
            }
            legacy_extension = home / "omp-agent/extensions/mobile-bridge.ts"
            legacy_extension.parent.mkdir(parents=True)
            legacy_extension.write_text("legacy")
            with (
                patch.dict(omp_deck.os.environ, environment, clear=False),
                patch.object(omp_deck, "CONFIG_PATH", config),
                patch.object(omp_deck, "SETTINGS", settings),
                patch.object(omp_deck.shutil, "which", side_effect=lambda name: f"/usr/bin/{name}"),
                patch.object(omp_deck, "run", side_effect=lambda command, check=True: calls.append(command)),
                patch.object(omp_deck, "enable_tailscale", return_value=None),
            ):
                omp_deck.install()

            unit = home / "config/systemd/user/omp-deck.service"
            command = home / "bin/omp-deck"
            self.assertTrue((home / "data/omp-deck/server.py").is_file())
            self.assertTrue((home / "omp-agent/extensions/omp-deck.ts").is_file())
            self.assertFalse(legacy_extension.exists())
            self.assertIn(str(home / "data/omp-deck/server.py"), unit.read_text())
            self.assertTrue(command.stat().st_mode & 0o100)
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
            self.assertIn(["systemctl", "--user", "enable", "omp-deck.service"], calls)
            self.assertIn(["systemctl", "--user", "restart", "omp-deck.service"], calls)

    def test_tailscale_is_enabled_only_when_unconfigured(self):
        results = [
            SimpleNamespace(returncode=0, stdout="{}"),
            SimpleNamespace(returncode=0, stdout=""),
            SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"Self": {"DNSName": "workstation.example.ts.net."}}),
            ),
        ]
        with (
            patch.object(omp_deck.shutil, "which", return_value="/usr/bin/tailscale"),
            patch.object(omp_deck.subprocess, "run", side_effect=results) as run,
        ):
            url = omp_deck.enable_tailscale()

        self.assertEqual(url, "https://workstation.example.ts.net")
        self.assertEqual(
            run.call_args_list[1].args[0],
            ["tailscale", "serve", "--bg", "--yes", "8787"],
        )


if __name__ == "__main__":
    unittest.main()
