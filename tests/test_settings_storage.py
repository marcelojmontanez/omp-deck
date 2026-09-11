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


if __name__ == "__main__":
    unittest.main()
