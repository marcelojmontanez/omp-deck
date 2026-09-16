import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from omp_deck.settings import load_settings
from omp_deck.usage import UsageService


class StorageSettingsTests(unittest.TestCase):
    def test_tilde_omp_executable_is_expanded_and_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            executable = home / "bin" / "omp"
            executable.parent.mkdir()
            executable.write_text(
                '#!/bin/sh\nprintf \'%s\\n\' \'{"reports":[{"provider":"tilde"}]}\'\n',
                encoding="utf-8",
            )
            executable.chmod(0o700)
            config = home / "config.json"
            config.write_text(json.dumps({"omp_bin": "~/bin/omp"}), encoding="utf-8")
            config.chmod(0o600)
            with patch.dict(os.environ, {"HOME": str(home)}):
                settings = load_settings(config)

            self.assertEqual(settings.omp_bin, str(executable))
            self.assertEqual(
                UsageService(settings.omp_bin).snapshot(),
                {"reports": [{"provider": "tilde"}]},
            )

    def test_bare_path_command_remains_bare(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            config.write_text(json.dumps({"omp_bin": "omp-custom"}), encoding="utf-8")
            config.chmod(0o600)

            self.assertEqual(load_settings(config).omp_bin, "omp-custom")

    def test_tailscale_auth_configuration_requires_one_owner_and_loopback_listener(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"

            for values in (
                {"auth_mode": "unknown"},
                {"auth_mode": "tailscale"},
                {"auth_mode": "tailscale", "tailscale_user": "  "},
                {"auth_mode": "pairing", "tailscale_user": "owner@example.com"},
                {"auth_mode": "tailscale", "tailscale_user": "owner@example.com", "host": "0.0.0.0"},
            ):
                with self.subTest(values=values):
                    config.write_text(json.dumps(values), encoding="utf-8")
                    config.chmod(0o600)
                    with self.assertRaises(RuntimeError):
                        load_settings(config)


if __name__ == "__main__":
    unittest.main()
