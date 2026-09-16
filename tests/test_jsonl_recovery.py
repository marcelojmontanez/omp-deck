import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from omp_deck.jsonl_recovery import (
    JsonlRecoveryReader,
    RecoveryIssue,
    RecoveryStatus,
)


class JsonlRecoveryReaderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.path = self.root / "sessions" / "active.jsonl"
        self.path.parent.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def reader(self, **limits):
        return JsonlRecoveryReader(self.root, "sessions/active.jsonl", **limits)

    def append(self, value: bytes):
        with self.path.open("ab") as stream:
            stream.write(value)

    def test_incremental_append_and_unchanged_poll_reads_no_bytes(self):
        self.path.write_bytes(b'{"sequence":1}\n')
        reader = self.reader()
        first = reader.poll()
        self.assertEqual(first.status, RecoveryStatus.OK)
        self.assertEqual(first.entries, ({"sequence": 1},))
        self.assertEqual(first.bytes_read, len(self.path.read_bytes()))

        with mock.patch("omp_deck.jsonl_recovery.os.read", wraps=os.read) as read:
            unchanged = reader.poll()
        self.assertEqual(unchanged.bytes_read, 0)
        read.assert_not_called()

        self.append(b'{"sequence":2}\n')
        appended = reader.poll()
        self.assertEqual(appended.entries, ({"sequence": 2},))
        self.assertEqual(appended.retained, ({"sequence": 1}, {"sequence": 2}))

    def test_partial_final_line_is_retained_without_rereading(self):
        self.path.write_bytes(b'{"message":"hel')
        reader = self.reader()
        first = reader.poll()
        self.assertEqual(first.entries, ())
        self.assertEqual(first.offset, 15)
        self.append(b'lo"}\n')
        second = reader.poll()
        self.assertEqual(second.entries, ({"message": "hello"},))
        self.assertEqual(second.bytes_read, 5)

    def test_read_budget_is_applied_per_poll(self):
        self.path.write_bytes(b'{"a":1}\n{"b":2}\n')
        reader = self.reader(max_read_bytes=8)
        first = reader.poll()
        second = reader.poll()
        third = reader.poll()
        self.assertEqual(first.bytes_read, 8)
        self.assertEqual(second.bytes_read, 8)
        self.assertEqual(third.bytes_read, 0)
        self.assertEqual(first.entries, ({"a": 1},))
        self.assertEqual(second.entries, ({"b": 2},))

    def test_exact_line_limit_is_accepted_and_over_limit_is_degraded(self):
        line = b'{"a":1}'
        self.path.write_bytes(line + b"\n")
        accepted = self.reader(max_line_bytes=len(line)).poll()
        self.assertEqual(accepted.entries, ({"a": 1},))

        self.path.write_bytes(line + b" \n" + b"{}\n")
        rejected = self.reader(max_line_bytes=len(line)).poll()
        self.assertEqual(rejected.status, RecoveryStatus.DEGRADED)
        self.assertEqual(rejected.issues, (RecoveryIssue.LINE_TOO_LONG,))
        self.assertEqual(rejected.entries, ({},))

    def test_overlong_line_across_polls_is_discarded_until_newline(self):
        self.path.write_bytes(b"12345")
        reader = self.reader(max_line_bytes=5, max_read_bytes=5)
        self.assertEqual(reader.poll().issues, ())
        self.append(b"6")
        overlong = reader.poll()
        self.assertEqual(overlong.issues, (RecoveryIssue.LINE_TOO_LONG,))
        self.append(b"789\n{}\n")
        recovered_entries = []
        for _ in range(3):
            recovered_entries.extend(reader.poll().entries)
        self.assertEqual(recovered_entries, [{}])

    def test_file_size_limit_prevents_reads_and_cursor_advance(self):
        self.path.write_bytes(b"12345")
        reader = self.reader(max_file_bytes=4)
        with mock.patch("omp_deck.jsonl_recovery.os.read", wraps=os.read) as read:
            result = reader.poll()
        self.assertEqual(result.status, RecoveryStatus.DEGRADED)
        self.assertEqual(result.issues, (RecoveryIssue.FILE_TOO_LARGE,))
        self.assertEqual(result.offset, 0)
        read.assert_not_called()

    def test_retained_entries_are_bounded_but_new_entries_are_reported(self):
        self.path.write_bytes(b'{"n":1}\n{"n":2}\n{"n":3}\n')
        result = self.reader(max_entries=2).poll()
        self.assertEqual(len(result.entries), 3)
        self.assertEqual(result.retained, ({"n": 2}, {"n": 3}))

    def test_malformed_and_non_object_lines_have_typed_outcomes(self):
        self.path.write_bytes(b'not-json\n[1,2]\n{"valid":true}\n')
        result = self.reader().poll()
        self.assertEqual(result.status, RecoveryStatus.DEGRADED)
        self.assertEqual(
            result.issues,
            (RecoveryIssue.MALFORMED_JSON, RecoveryIssue.NON_OBJECT_JSON),
        )
        self.assertEqual(result.entries, ({"valid": True},))

    def test_missing_file_is_explicit_and_can_later_appear(self):
        reader = self.reader()
        self.assertEqual(reader.poll().status, RecoveryStatus.MISSING)
        self.path.write_bytes(b'{"appeared":true}\n')
        self.assertEqual(reader.poll().entries, ({"appeared": True},))

    def test_rotation_resets_cursor_partial_and_retained_state(self):
        self.path.write_bytes(b'{"old":1}\n{"partial"')
        reader = self.reader()
        reader.poll()
        replacement = self.path.with_suffix(".new")
        replacement.write_bytes(b'{"new":1}\n')
        replacement.replace(self.path)
        result = reader.poll()
        self.assertEqual(result.status, RecoveryStatus.ROTATED)
        self.assertEqual(result.entries, ({"new": 1},))
        self.assertEqual(result.retained, ({"new": 1},))

    def test_truncation_resets_cursor_and_retained_state(self):
        self.path.write_bytes(b'{"old":"long value"}\n')
        reader = self.reader()
        reader.poll()
        self.path.write_bytes(b'{"n":1}\n')
        result = reader.poll()
        self.assertEqual(result.status, RecoveryStatus.TRUNCATED)
        self.assertEqual(result.entries, ({"n": 1},))
        self.assertEqual(result.retained, ({"n": 1},))

    def test_rejects_absolute_and_traversing_paths(self):
        for path in (self.path, "../active.jsonl", "sessions/../active.jsonl"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                JsonlRecoveryReader(self.root, path)

    def test_rejects_symlink_file_and_symlink_directory(self):
        outside = self.root.parent / (self.root.name + "-outside")
        outside.mkdir()
        self.addCleanup(lambda: outside.rmdir())
        target = outside / "target.jsonl"
        target.write_bytes(b'{"secret":true}\n')
        self.addCleanup(target.unlink)

        self.path.symlink_to(target)
        file_result = self.reader().poll()
        self.assertEqual(file_result.status, RecoveryStatus.UNSAFE_PATH)
        self.path.unlink()
        self.path.parent.rmdir()
        (self.root / "sessions").symlink_to(outside, target_is_directory=True)
        directory_result = self.reader().poll()
        self.assertEqual(directory_result.status, RecoveryStatus.UNSAFE_PATH)

    def test_replacement_during_read_is_rejected_without_advancing_cursor(self):
        self.path.write_bytes(b'{"old":1}\n')
        replacement = self.path.with_suffix(".new")
        replacement.write_bytes(b'{"new":1}\n')
        real_read = os.read

        def read_then_replace(fd, amount):
            data = real_read(fd, amount)
            replacement.replace(self.path)
            return data

        reader = self.reader()
        with mock.patch("omp_deck.jsonl_recovery.os.read", side_effect=read_then_replace):
            result = reader.poll()
        self.assertEqual(result.status, RecoveryStatus.UNSAFE_PATH)
        self.assertEqual(result.issues, (RecoveryIssue.PATH_CHANGED,))
        self.assertEqual(result.offset, 0)
        self.assertEqual(result.entries, ())

    def test_path_revalidation_error_closes_every_open_descriptor(self):
        self.path.write_bytes(b'{"value":1}\n')
        reader = self.reader()
        opened = []
        real_open = os.open

        def tracked_open(*args, **kwargs):
            descriptor = real_open(*args, **kwargs)
            opened.append(descriptor)
            return descriptor

        try:
            with (
                mock.patch("omp_deck.jsonl_recovery.os.open", side_effect=tracked_open),
                mock.patch("omp_deck.jsonl_recovery.os.stat", side_effect=OSError("path unavailable")),
            ):
                result = reader.poll()

            self.assertEqual(result.status, RecoveryStatus.UNSAFE_PATH)
            self.assertEqual(result.issues, (RecoveryIssue.IO_ERROR,))
            for descriptor in opened:
                with self.assertRaises(OSError):
                    os.fstat(descriptor)
        finally:
            for descriptor in opened:
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def test_non_regular_file_is_rejected(self):
        self.path.mkdir()
        result = self.reader().poll()
        self.assertEqual(result.status, RecoveryStatus.UNSAFE_PATH)
        self.assertEqual(result.issues, (RecoveryIssue.NOT_REGULAR_FILE,))

    def test_invalid_limits_are_rejected(self):
        self.path.write_bytes(b"")
        for name in ("max_file_bytes", "max_line_bytes", "max_entries", "max_read_bytes"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.reader(**{name: 0})


if __name__ == "__main__":
    unittest.main()
