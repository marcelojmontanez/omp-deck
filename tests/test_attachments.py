import hashlib
import os
import subprocess
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
        if os.name == "nt":
            from omp_deck._windows_security import secure_created_path

            secure_created_path(self.root)
        self.patch = patch.object(attachments, "BLOB_ROOT", self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.payload = b"\x89PNG\r\n\x1a\nimage"
        self.digest = hashlib.sha256(self.payload).hexdigest()

    def write_blob(self, path, payload):
        path.write_bytes(payload)
        if os.name == "nt":
            from omp_deck._windows_security import secure_created_path

            secure_created_path(path)

    def test_reads_one_bounded_regular_blob(self):
        candidate = self.root / f"{self.digest}.png"
        self.write_blob(candidate, self.payload)
        with patch.object(
            attachments.os,
            "scandir",
            side_effect=AssertionError("blob lookup must not scan the directory"),
        ):
            result = attachments.read_blob(self.digest)
        self.assertEqual(
            result,
            (f"{self.digest}.png", self.payload),
        )

    def test_duplicate_content_addressed_names_resolve_to_verified_content(self):
        self.write_blob(self.root / self.digest, self.payload)
        self.write_blob(self.root / f"{self.digest}.png", self.payload)

        self.assertEqual(
            attachments.read_blob(self.digest),
            (self.digest, self.payload),
        )

    def test_rejects_malformed_symlink_poisoned_and_oversized_blobs(self):
        self.assertIsNone(attachments.read_blob("../private"))

        if os.name != "nt":
            target = self.root / "target.png"
            target.write_bytes(b"private")
            symlink = self.root / f"{self.digest}.png"
            symlink.symlink_to(target)
            self.assertIsNone(attachments.read_blob(self.digest))
            symlink.unlink()

        poisoned = self.root / f"{self.digest}.png"
        self.write_blob(poisoned, b"not the addressed content")
        self.assertIsNone(attachments.read_blob(self.digest))

        poisoned.write_bytes(b"")
        self.assertIsNone(attachments.read_blob(self.digest))
        poisoned.unlink()
        oversized = self.root / f"{self.digest}.png"
        with oversized.open("wb") as stream:
            stream.truncate(16 * 1024 * 1024 + 1)
        if os.name == "nt":
            from omp_deck._windows_security import secure_created_path

            secure_created_path(oversized)
        self.assertIsNone(attachments.read_blob(self.digest))

    def test_rejects_writable_blob_root_and_candidate(self):
        candidate = self.root / f"{self.digest}.png"
        self.write_blob(candidate, self.payload)
        if os.name == "nt":
            from omp_deck._windows_security import secure_path

            subprocess.run(
                ["icacls", str(self.root), "/grant", "*S-1-1-0:(OI)(CI)M"],
                check=True,
                capture_output=True,
            )
            self.assertIsNone(attachments.read_blob(self.digest))
            secure_path(self.root)
            subprocess.run(
                ["icacls", str(candidate), "/grant", "*S-1-1-0:M"],
                check=True,
                capture_output=True,
            )
            self.assertIsNone(attachments.read_blob(self.digest))
            return
        candidate.chmod(0o666)
        self.assertIsNone(attachments.read_blob(self.digest))

        candidate.chmod(0o600)
        self.root.chmod(0o777)
        self.assertIsNone(attachments.read_blob(self.digest))

    def test_image_mime_is_derived_from_file_signature(self):
        self.assertEqual(attachments.detect_image_mime(b"\x89PNG\r\n\x1a\nrest"), "image/png")
        self.assertEqual(attachments.detect_image_mime(b"\xff\xd8\xffrest"), "image/jpeg")
        self.assertEqual(attachments.detect_image_mime(b"GIF89arest"), "image/gif")
        self.assertEqual(attachments.detect_image_mime(b"RIFF\x00\x00\x00\x00WEBPrest"), "image/webp")
        self.assertIsNone(attachments.detect_image_mime(b"not-an-image"))


class BlobDirectoryResolutionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.payload = b"\x89PNG\r\n\x1a\nprofile image"
        self.digest = hashlib.sha256(self.payload).hexdigest()

    def _write_blob(self, root):
        root.mkdir(parents=True, mode=0o700)
        root.chmod(0o700)
        candidate = root / f"{self.digest}.png"
        candidate.write_bytes(self.payload)
        candidate.chmod(0o600)

    def test_reads_blob_from_official_named_profile_directory(self):
        blobs = self.root / ".omp" / "profiles" / "work" / "agent" / "blobs"
        self._write_blob(blobs)
        with (
            patch.object(attachments, "BLOB_ROOT", None),
            patch.object(attachments.Path, "home", return_value=self.root),
            patch.dict(os.environ, {"OMP_PROFILE": "work"}, clear=True),
        ):
            self.assertEqual(
                attachments.read_blob(self.digest),
                (f"{self.digest}.png", self.payload),
            )

    def test_reads_blob_from_existing_official_xdg_root(self):
        data_home = self.root / "data"
        blobs = data_home / "omp" / "blobs"
        self._write_blob(blobs)
        with (
            patch.object(attachments, "BLOB_ROOT", None),
            patch.object(attachments.Path, "home", return_value=self.root),
            patch.dict(os.environ, {"XDG_DATA_HOME": str(data_home)}, clear=True),
        ):
            self.assertEqual(
                attachments.read_blob(self.digest),
                (f"{self.digest}.png", self.payload),
            )

    @unittest.skipIf(os.name == "nt", "POSIX symlink confinement")
    def test_rejects_symlinked_official_blob_root(self):
        outside = self.root / "outside"
        self._write_blob(outside)
        agent = self.root / ".omp" / "agent"
        agent.mkdir(parents=True)
        (agent / "blobs").symlink_to(outside, target_is_directory=True)
        with (
            patch.object(attachments, "BLOB_ROOT", None),
            patch.object(attachments.Path, "home", return_value=self.root),
            patch.dict(os.environ, {}, clear=True),
        ):
            self.assertIsNone(attachments.read_blob(self.digest))

    @unittest.skipIf(os.name == "nt", "POSIX symlink confinement")
    def test_rejects_blob_root_beneath_symlinked_profile_parent(self):
        actual = self.root / "actual-profile"
        self._write_blob(actual / "agent" / "blobs")
        (self.root / ".omp").symlink_to(actual, target_is_directory=True)
        with (
            patch.object(attachments, "BLOB_ROOT", None),
            patch.object(attachments.Path, "home", return_value=self.root),
            patch.dict(os.environ, {}, clear=True),
        ):
            self.assertIsNone(attachments.read_blob(self.digest))


if __name__ == "__main__":
    unittest.main()
