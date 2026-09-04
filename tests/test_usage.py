import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from omp_deck.usage import UsageService


class UsageServiceTests(unittest.TestCase):
    @patch("omp_deck.usage.subprocess.run")
    def test_uses_omp_json_command_without_shell_and_caches_result(self, run):
        payload = {"reports": [{"provider": "example", "limits": []}]}
        run.return_value = SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
        service = UsageService("/usr/bin/omp", ttl=60)

        self.assertEqual(service.snapshot(), payload)
        self.assertEqual(service.snapshot(), payload)

        run.assert_called_once_with(
            ["/usr/bin/omp", "usage", "--json"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )

    @patch("omp_deck.usage.subprocess.run")
    def test_command_failure_is_generic_and_does_not_leak_stderr(self, run):
        run.return_value = SimpleNamespace(returncode=1, stdout="", stderr="private path and token")

        result = UsageService("omp", ttl=0).snapshot()

        self.assertEqual(result, {"reports": [], "error": "Usage is temporarily unavailable"})
        self.assertNotIn("private", str(result))


if __name__ == "__main__":
    unittest.main()
