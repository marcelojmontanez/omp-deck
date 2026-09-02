import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import process_discovery


class ParentSessionIdTests(unittest.TestCase):
    PARENT_ID = "01a057eb-4443-7340-9f5f-bee91cd3d986"

    def test_nested_session_uses_uuid_suffix_of_parent_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = root / "project" / f"2026-08-31T13-03-51Z_{self.PARENT_ID.upper()}" / "worker.jsonl"

            self.assertEqual(
                process_discovery.parent_session_id(session, root),
                self.PARENT_ID,
            )

    def test_top_level_session_has_no_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = root / "project" / f"started_{self.PARENT_ID}.jsonl"

            self.assertIsNone(process_discovery.parent_session_id(session, root))

    def test_malformed_nested_parent_and_outside_path_have_no_parent(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root = Path(directory)

            self.assertIsNone(
                process_discovery.parent_session_id(
                    root / "project" / f"prefix_{self.PARENT_ID}_suffix" / "worker.jsonl",
                    root,
                )
            )
            self.assertIsNone(
                process_discovery.parent_session_id(
                    Path(outside) / f"parent_{self.PARENT_ID}" / "worker.jsonl",
                    root,
                )
            )




if __name__ == "__main__":
    unittest.main()
