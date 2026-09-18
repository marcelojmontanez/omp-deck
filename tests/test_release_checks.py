import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.release_lib import check_receipt


class CheckReceiptTests(unittest.TestCase):
    def test_git_hidden_source_edit_cannot_reuse_a_successful_gate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            subprocess.run(["git", "init", "--quiet", str(root)], check=True, capture_output=True)
            source = root / "sample.py"
            source.write_text("print('checked')\n")
            subprocess.run(["git", "add", "sample.py"], cwd=root, check=True, capture_output=True)
            before = check_receipt.fingerprint(root)
            self.assertIsNotNone(before)
            check_receipt.record(root, before, release=False)
            self.assertTrue(check_receipt.valid(root))
            subprocess.run(
                ["git", "update-index", "--assume-unchanged", "sample.py"], cwd=root, check=True, capture_output=True
            )
            source.write_text("print('unchecked')\n")
            self.assertFalse(check_receipt.valid(root))
