import hashlib
import os
import re
import stat
import sys
from pathlib import Path

BLOB_ROOT = None
_DIGEST = re.compile(r"[a-f0-9]{64}")
_PROFILE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_MAX_BLOB_BYTES = 16 * 1024 * 1024
_CANDIDATE_SUFFIXES = ("", ".png", ".jpg", ".jpeg", ".gif", ".webp")


def blob_root():
    if BLOB_ROOT is not None:
        return Path(BLOB_ROOT)
    profile_value = os.environ.get("OMP_PROFILE")
    if profile_value is None:
        profile_value = os.environ.get("PI_PROFILE")
    profile = profile_value.strip() if profile_value else None
    if profile == "default":
        profile = None
    if (
        profile in (".", "..")
        or (profile is not None and profile.endswith("."))
        or (profile is not None and _PROFILE.fullmatch(profile) is None)
    ):
        profile = None
    home = Path.home()
    config_root = home / ".omp"
    if profile:
        config_root = config_root / "profiles" / profile
        agent_root = config_root / "agent"
    else:
        override = os.environ.get("PI_CODING_AGENT_DIR")
        agent_root = Path(override).expanduser().absolute() if override else config_root / "agent"
    if sys.platform in ("linux", "darwin"):
        xdg_value = os.environ.get("XDG_DATA_HOME")
        if xdg_value and agent_root == config_root / "agent":
            xdg_root = Path(xdg_value) / "omp"
            candidate = xdg_root / "profiles" / profile if profile else xdg_root
            if candidate.exists():
                return candidate / "blobs"
    return agent_root / "blobs"


def read_blob(digest):
    if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
        return None
    if os.name == "nt":
        from ._windows_files import read_user_owned_file

        root = blob_root()
        for suffix in _CANDIDATE_SUFFIXES:
            name = f"{digest}{suffix}"
            payload = read_user_owned_file(root, root / name, _MAX_BLOB_BYTES)
            if payload is not None and hashlib.sha256(payload).hexdigest() == digest:
                return name, payload
        return None
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        directory_fd = os.open(blob_root(), flags)
    except OSError:
        return None
    try:
        root_info = os.fstat(directory_fd)
        if not stat.S_ISDIR(root_info.st_mode) or root_info.st_uid != os.geteuid() or root_info.st_mode & 0o022:
            return None
        for suffix in _CANDIDATE_SUFFIXES:
            name = f"{digest}{suffix}"
            result = _read_candidate(directory_fd, name, digest)
            if result is not None:
                return result
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
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o022
            or not 0 < info.st_size <= _MAX_BLOB_BYTES
        ):
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
