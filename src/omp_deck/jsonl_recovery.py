"""Bounded, compatibility-only recovery of a configured JSONL session file.

This reader is deliberately not a discovery mechanism: callers supply both the
validated session root and a relative file name.  A reader instance owns the
cursor for that one file.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any


class RecoveryStatus(str, Enum):
    OK = "ok"
    MISSING = "missing"
    ROTATED = "rotated"
    TRUNCATED = "truncated"
    DEGRADED = "degraded"
    UNSAFE_PATH = "unsafe_path"


class RecoveryIssue(str, Enum):
    MALFORMED_JSON = "malformed_json"
    NON_OBJECT_JSON = "non_object_json"
    LINE_TOO_LONG = "line_too_long"
    FILE_TOO_LARGE = "file_too_large"
    NOT_REGULAR_FILE = "not_regular_file"
    PATH_CHANGED = "path_changed"
    IO_ERROR = "io_error"


@dataclass(frozen=True)
class RecoveryPoll:
    status: RecoveryStatus
    entries: tuple[dict[str, Any], ...] = ()
    retained: tuple[dict[str, Any], ...] = ()
    issues: tuple[RecoveryIssue, ...] = ()
    bytes_read: int = 0
    offset: int = 0


class JsonlRecoveryReader:
    """Incrementally read one JSONL file beneath a fixed, validated root."""

    def __init__(
        self,
        session_root: str | os.PathLike[str],
        relative_path: str | os.PathLike[str],
        *,
        max_file_bytes: int = 64 * 1024 * 1024,
        max_line_bytes: int = 1024 * 1024,
        max_entries: int = 10_000,
        max_read_bytes: int = 1024 * 1024,
    ) -> None:
        for name, value in (
            ("max_file_bytes", max_file_bytes),
            ("max_line_bytes", max_line_bytes),
            ("max_entries", max_entries),
            ("max_read_bytes", max_read_bytes),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")

        root = Path(session_root).resolve(strict=True)
        if not root.is_dir():
            raise ValueError("session_root must be a directory")
        relative = Path(relative_path)
        if relative.is_absolute() or not relative.parts or any(
            part in ("", ".", "..") for part in relative.parts
        ):
            raise ValueError("relative_path must stay beneath session_root")

        self._root = root
        root_stat = root.stat()
        self._root_identity = (root_stat.st_dev, root_stat.st_ino)
        self._parts = relative.parts
        self._max_file_bytes = max_file_bytes
        self._max_line_bytes = max_line_bytes
        self._max_entries = max_entries
        self._max_read_bytes = max_read_bytes
        self._identity: tuple[int, int] | None = None
        self._offset = 0
        self._partial = b""
        self._dropping_line = False
        self._retained: list[dict[str, Any]] = []

    @property
    def offset(self) -> int:
        return self._offset

    @property
    def retained(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._retained)

    def _result(
        self,
        status: RecoveryStatus,
        *,
        entries: list[dict[str, Any]] | None = None,
        issues: list[RecoveryIssue] | None = None,
        bytes_read: int = 0,
    ) -> RecoveryPoll:
        return RecoveryPoll(
            status=status,
            entries=tuple(entries or ()),
            retained=tuple(self._retained),
            issues=tuple(issues or ()),
            bytes_read=bytes_read,
            offset=self._offset,
        )

    def _open_file(self) -> tuple[int, os.stat_result]:
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow
        directory_fd = os.open(self._root, directory_flags)
        try:
            root_now = os.fstat(directory_fd)
            if (root_now.st_dev, root_now.st_ino) != self._root_identity:
                raise _UnsafePath
            for component in self._parts[:-1]:
                next_fd = os.open(component, directory_flags, dir_fd=directory_fd)
                os.close(directory_fd)
                directory_fd = next_fd
            file_fd = os.open(self._parts[-1], os.O_RDONLY | nofollow, dir_fd=directory_fd)
            file_stat = os.fstat(file_fd)
            if not stat.S_ISREG(file_stat.st_mode):
                os.close(file_fd)
                raise _NotRegularFile
            # Revalidate the directory entry after open.  This detects a replace
            # race rather than silently reading an unlinked or different file.
            path_stat = os.stat(
                self._parts[-1], dir_fd=directory_fd, follow_symlinks=False
            )
            if (path_stat.st_dev, path_stat.st_ino) != (
                file_stat.st_dev,
                file_stat.st_ino,
            ):
                os.close(file_fd)
                raise _UnsafePath
            return file_fd, file_stat
        finally:
            os.close(directory_fd)

    def _reset_for_new_file(self, identity: tuple[int, int]) -> None:
        self._identity = identity
        self._offset = 0
        self._partial = b""
        self._dropping_line = False
        self._retained.clear()

    def poll(self) -> RecoveryPoll:
        try:
            fd, before = self._open_file()
        except FileNotFoundError:
            return self._result(RecoveryStatus.MISSING)
        except _NotRegularFile:
            return self._result(
                RecoveryStatus.UNSAFE_PATH, issues=[RecoveryIssue.NOT_REGULAR_FILE]
            )
        except (_UnsafePath, OSError) as error:
            issue = RecoveryIssue.PATH_CHANGED if isinstance(error, _UnsafePath) else RecoveryIssue.IO_ERROR
            return self._result(RecoveryStatus.UNSAFE_PATH, issues=[issue])

        identity = (before.st_dev, before.st_ino)
        reset_status: RecoveryStatus | None = None
        if self._identity is None:
            self._identity = identity
        elif identity != self._identity:
            self._reset_for_new_file(identity)
            reset_status = RecoveryStatus.ROTATED
        elif before.st_size < self._offset:
            self._reset_for_new_file(identity)
            reset_status = RecoveryStatus.TRUNCATED

        if before.st_size > self._max_file_bytes:
            os.close(fd)
            return self._result(
                RecoveryStatus.DEGRADED, issues=[RecoveryIssue.FILE_TOO_LARGE]
            )

        amount = min(self._max_read_bytes, before.st_size - self._offset)
        if amount <= 0:
            os.close(fd)
            return self._result(reset_status or RecoveryStatus.OK)

        try:
            os.lseek(fd, self._offset, os.SEEK_SET)
            data = os.read(fd, amount)
            after = os.fstat(fd)
            path_after = os.stat(self._root.joinpath(*self._parts), follow_symlinks=False)
        except OSError:
            os.close(fd)
            return self._result(RecoveryStatus.DEGRADED, issues=[RecoveryIssue.IO_ERROR])
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

        if (after.st_dev, after.st_ino) != identity or (
            path_after.st_dev,
            path_after.st_ino,
        ) != identity:
            return self._result(
                RecoveryStatus.UNSAFE_PATH,
                issues=[RecoveryIssue.PATH_CHANGED],
                bytes_read=len(data),
            )

        self._offset += len(data)
        entries: list[dict[str, Any]] = []
        issues: list[RecoveryIssue] = []
        chunks = data.split(b"\n")
        complete, tail = chunks[:-1], chunks[-1]
        for chunk in complete:
            if self._dropping_line:
                self._dropping_line = False
                continue
            line = self._partial + chunk
            self._partial = b""
            if len(line) > self._max_line_bytes:
                issues.append(RecoveryIssue.LINE_TOO_LONG)
                continue
            self._decode_line(line, entries, issues)

        if self._dropping_line:
            pass
        elif len(self._partial) + len(tail) > self._max_line_bytes:
            self._partial = b""
            self._dropping_line = True
            issues.append(RecoveryIssue.LINE_TOO_LONG)
        else:
            self._partial += tail

        if entries:
            self._retained.extend(entries)
            if len(self._retained) > self._max_entries:
                del self._retained[: len(self._retained) - self._max_entries]
        status = RecoveryStatus.DEGRADED if issues else (reset_status or RecoveryStatus.OK)
        return self._result(status, entries=entries, issues=issues, bytes_read=len(data))

    @staticmethod
    def _decode_line(
        line: bytes,
        entries: list[dict[str, Any]],
        issues: list[RecoveryIssue],
    ) -> None:
        if not line.strip():
            return
        try:
            value = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            issues.append(RecoveryIssue.MALFORMED_JSON)
            return
        if not isinstance(value, dict):
            issues.append(RecoveryIssue.NON_OBJECT_JSON)
            return
        entries.append(value)


class _UnsafePath(Exception):
    pass


class _NotRegularFile(Exception):
    pass
