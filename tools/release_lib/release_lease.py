from __future__ import annotations

import contextlib
import os
import stat
from collections.abc import Iterator
from pathlib import Path

from .storage import ReleaseError

if os.name != "nt":
    import fcntl


@contextlib.contextmanager
def operator_lock(path: Path) -> Iterator[None]:
    """Hold an OS-owned release lease for exactly one process incarnation."""
    if os.name == "nt":
        raise ReleaseError("release automation requires macOS or Linux")
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ReleaseError("unsafe operator lease lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ReleaseError("another release operation is still running") from error
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
