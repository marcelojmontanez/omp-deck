"""Small stdlib-only bootstrap used before the immutable worker is uploaded."""

import json
import os
import stat
import sys
import tempfile
from pathlib import Path

LIMIT = 128 * 1024 * 1024


def owned_file(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_size > LIMIT:
        os.close(descriptor)
        raise ValueError("unsafe transfer source")
    return descriptor, info.st_size


def private_directory(path):
    missing = []
    current = path
    while not current.exists() and not current.is_symlink():
        missing.append(current)
        current = current.parent
    for item in reversed(missing):
        item.mkdir(mode=0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError("unsafe transfer directory")


def upload(header):
    root = Path(header["root"])
    relative = Path(header["name"])
    if root.resolve() != root:
        raise ValueError("transfer root cannot traverse symlinks")
    if not root.is_absolute() or ".." in root.parts or relative.is_absolute() or ".." in relative.parts:
        raise ValueError("invalid transfer path")
    private_directory(root)
    current = root
    for part in relative.parts[:-1]:
        current /= part
        private_directory(current)
    destination = current / relative.name
    if destination.is_symlink():
        raise ValueError("symlink transfer destination")
    size = header["size"]
    if not isinstance(size, int) or not 0 <= size <= LIMIT:
        raise ValueError("invalid transfer size")
    descriptor, temporary = tempfile.mkstemp(prefix=".upload-", dir=current)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            remaining = size
            while remaining:
                chunk = sys.stdin.buffer.read(min(65536, remaining))
                if not chunk:
                    raise ValueError("incomplete transfer")
                stream.write(chunk)
                remaining -= len(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main():
    header = json.loads(sys.stdin.buffer.readline(16385))
    if header.get("operation") == "upload":
        upload(header)
    elif header.get("operation") == "download":
        path = Path(header["path"])
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("invalid transfer path")
        descriptor, size = owned_file(path)
        with os.fdopen(descriptor, "rb") as stream:
            while size:
                chunk = stream.read(min(size, 65536))
                if not chunk:
                    raise ValueError("incomplete transfer")
                sys.stdout.buffer.write(chunk)
                size -= len(chunk)
    else:
        raise ValueError("unknown transfer operation")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("private transfer failed", file=sys.stderr)
        sys.exit(1)
