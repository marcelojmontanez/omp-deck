import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from omp_deck import attachments


class BlobReaderTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.patch = patch.object(attachments, "BLOB_ROOT", self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.digest = "a" * 64

    def test_reads_one_bounded_regular_blob(self):
        (self.root / f"{self.digest}.png").write_bytes(b"image")
        self.assertEqual(attachments.read_blob(self.digest), (f"{self.digest}.png", b"image"))

    def test_rejects_malformed_ambiguous_symlink_and_oversized_blobs(self):
        self.assertIsNone(attachments.read_blob("../private"))

        target = self.root / "target.png"
        target.write_bytes(b"private")
        symlink = self.root / f"{self.digest}.png"
        symlink.symlink_to(target)
        self.assertIsNone(attachments.read_blob(self.digest))
        symlink.unlink()

        first = self.root / f"{self.digest}.png"
        second = self.root / f"{self.digest}.jpg"
        first.write_bytes(b"one")
        second.write_bytes(b"two")
        self.assertIsNone(attachments.read_blob(self.digest))
        second.unlink()

        first.write_bytes(b"")
        self.assertIsNone(attachments.read_blob(self.digest))
        first.unlink()
        oversized = self.root / f"{self.digest}.bin"
        with oversized.open("wb") as stream:
            stream.truncate(16 * 1024 * 1024 + 1)
        self.assertIsNone(attachments.read_blob(self.digest))


if __name__ == "__main__":
    unittest.main()
