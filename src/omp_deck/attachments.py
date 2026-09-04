import os
import hashlib
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
    try:
        names = [
            entry.name
            for entry in os.scandir(directory_fd)
            if entry.name == digest or entry.name.startswith(f"{digest}.")
        ]
        if not names or len(names) > 16:
            return None
        for name in sorted(names, key=lambda candidate: (candidate != digest, candidate)):
            result = _read_candidate(directory_fd, name, digest)
            if result is not None:
                return result
        return None
    except OSError:
        return None
    finally:
        os.close(directory_fd)


def _read_candidate(directory_fd, name, digest):
    descriptor = -1
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_fd,
        )
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= _MAX_BLOB_BYTES:
            return None
        body = bytearray()
        while len(body) < info.st_size:
            chunk = os.read(descriptor, info.st_size - len(body))
            if not chunk:
                return None
            body.extend(chunk)
        payload = bytes(body)
        if hashlib.sha256(payload).hexdigest() != digest:
            return None
        return name, payload
    except OSError:
        return None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def detect_image_mime(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None
