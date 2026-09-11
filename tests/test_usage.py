import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
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

    def test_concurrent_cache_misses_share_one_real_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            count = root / "count"
            executable = root / "omp"
            executable.write_text(
                "#!/bin/sh\n"
                f"printf x >> '{count}'\n"
                "sleep 0.2\n"
                'printf \'%s\\n\' \'{"reports":[{"provider":"shared"}]}\'\n',
                encoding="utf-8",
            )
            executable.chmod(0o700)
            service = UsageService(str(executable), ttl=60)
            barrier = threading.Barrier(4)

            def snapshot():
                barrier.wait()
                return service.snapshot()

            with ThreadPoolExecutor(max_workers=4) as executor:
                results = list(executor.map(lambda _: snapshot(), range(4)))

            self.assertEqual(count.read_text(encoding="utf-8"), "x")
            self.assertEqual(results, [{"reports": [{"provider": "shared"}]}] * 4)

    def test_failed_shared_process_releases_all_waiters(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            count = root / "count"
            executable = root / "omp"
            executable.write_text(
                f"#!/bin/sh\nprintf x >> '{count}'\nsleep 0.2\nexit 1\n",
                encoding="utf-8",
            )
            executable.chmod(0o700)
            service = UsageService(str(executable), ttl=60)
            barrier = threading.Barrier(4)
            with ThreadPoolExecutor(max_workers=4) as executor:
                futures = [executor.submit(lambda: (barrier.wait(), service.snapshot())[1]) for _ in range(4)]
                results = [future.result(timeout=3) for future in futures]

            unavailable = {"reports": [], "error": "Usage is temporarily unavailable"}
            self.assertEqual(count.read_text(encoding="utf-8"), "x")
            self.assertEqual(results, [unavailable] * 4)


if __name__ == "__main__":
    unittest.main()
