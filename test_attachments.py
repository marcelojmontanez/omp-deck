import tempfile
import hashlib
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
        self.payload = b"\x89PNG\r\n\x1a\nimage"
        self.digest = hashlib.sha256(self.payload).hexdigest()

    def test_reads_one_bounded_regular_blob(self):
        (self.root / f"{self.digest}.png").write_bytes(self.payload)
        self.assertEqual(
            attachments.read_blob(self.digest),
            (f"{self.digest}.png", self.payload),
        )

    def test_duplicate_content_addressed_names_resolve_to_verified_content(self):
        (self.root / self.digest).write_bytes(self.payload)
        (self.root / f"{self.digest}.png").write_bytes(self.payload)

        self.assertEqual(
            attachments.read_blob(self.digest),
            (self.digest, self.payload),
        )

    def test_rejects_malformed_symlink_poisoned_and_oversized_blobs(self):
        self.assertIsNone(attachments.read_blob("../private"))

        target = self.root / "target.png"
        target.write_bytes(b"private")
        symlink = self.root / f"{self.digest}.png"
        symlink.symlink_to(target)
        self.assertIsNone(attachments.read_blob(self.digest))
        symlink.unlink()

        poisoned = self.root / f"{self.digest}.png"
        poisoned.write_bytes(b"not the addressed content")
        self.assertIsNone(attachments.read_blob(self.digest))

        poisoned.write_bytes(b"")
        self.assertIsNone(attachments.read_blob(self.digest))
        poisoned.unlink()
        oversized = self.root / f"{self.digest}.bin"
        with oversized.open("wb") as stream:
            stream.truncate(16 * 1024 * 1024 + 1)
        self.assertIsNone(attachments.read_blob(self.digest))

    def test_image_mime_is_derived_from_file_signature(self):
        self.assertEqual(attachments.detect_image_mime(b"\x89PNG\r\n\x1a\nrest"), "image/png")
        self.assertEqual(attachments.detect_image_mime(b"\xff\xd8\xffrest"), "image/jpeg")
        self.assertEqual(attachments.detect_image_mime(b"GIF89arest"), "image/gif")
        self.assertEqual(attachments.detect_image_mime(b"RIFF\x00\x00\x00\x00WEBPrest"), "image/webp")
        self.assertIsNone(attachments.detect_image_mime(b"not-an-image"))


if __name__ == "__main__":
    unittest.main()
