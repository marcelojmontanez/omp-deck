import os
import re
import stat
from pathlib import Path


BLOB_ROOT = None
_DIGEST = re.compile(r"[a-f0-9]{64}")
_MAX_BLOB_BYTES = 16 * 1024 * 1024


def blob_root():
    return Path(BLOB_ROOT) if BLOB_ROOT is not None else Path.home() / ".omp/agent/blobs"


def read_blob(digest):
    if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
        return None
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        directory_fd = os.open(blob_root(), flags)
    except OSError:
        return None
    descriptor = -1
    try:
        names = [
            entry.name
            for entry in os.scandir(directory_fd)
            if entry.name == digest or entry.name.startswith(f"{digest}.")
        ]
        if len(names) != 1:
            return None
        descriptor = os.open(
            names[0],
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_fd,
        )
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= _MAX_BLOB_BYTES:
            return None
        body = os.read(descriptor, info.st_size + 1)
        return (names[0], body) if len(body) == info.st_size else None
    except OSError:
        return None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(directory_fd)
