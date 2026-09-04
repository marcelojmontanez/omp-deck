from __future__ import annotations

import os
from pathlib import Path
import subprocess
import unittest


TEST_ROOT = Path(__file__).resolve().parent
ROOT = TEST_ROOT.parent
NODE_TEST = TEST_ROOT / "browser_behavior_test.mjs"


class BrowserBehaviorTests(unittest.TestCase):
    def test_node_browser_behavior_suite(self):
        environment = os.environ.copy()
        environment.pop("NODE_OPTIONS", None)
        environment.update({"LC_ALL": "C", "TZ": "UTC"})
        completed = subprocess.run(
            ["node", "--test", "--test-concurrency=1", str(NODE_TEST)],
            cwd=ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 0, output)


if __name__ == "__main__":
    unittest.main()
