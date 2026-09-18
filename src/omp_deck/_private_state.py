from __future__ import annotations

import os
import re
import secrets
import stat
from contextlib import contextmanager
from pathlib import Path

from ._atomic_file import replace_bytes as replace_bytes_at

if os.name == "nt":
    from ._windows_security import (
        is_private_path,
        named_mutex,
        owned_by_current_user,
        secure_created_path,
        secure_path,
    )
else:
    import fcntl

_TEMPORARY_ATTEMPTS = 16
_TEMPORARY_CLEANUP_BATCH = 4_096
_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_FILE_READ_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)


def _posix_owned(info: os.stat_result) -> bool:
    return info.st_uid == os.geteuid()


def _unsupported_path(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))


def ensure_private_directory(path: Path, *, repair: bool = True) -> None:
    path = Path(path)
    created = False
    try:
        info = path.lstat()
    except FileNotFoundError:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.mkdir(mode=0o700)
            created = True
        except FileExistsError:
            pass
        info = path.lstat()
    if _unsupported_path(info) or not stat.S_ISDIR(info.st_mode):
        raise OSError(f"private state root is unsafe: {path}")
    if os.name == "nt":
        if created:
            secure_created_path(path)
        elif not owned_by_current_user(path):
            raise PermissionError(f"private state root is not owned by the current user: {path}")
        elif repair:
            secure_path(path)
        elif not is_private_path(path):
            raise OSError(f"private state root has unsafe permissions: {path}")
        return
    if not _posix_owned(info):
        raise PermissionError(f"private state root is not owned by the current user: {path}")
    if created or repair:
        path.chmod(0o700)
    elif stat.S_IMODE(info.st_mode) != 0o700:
        raise OSError(f"private state root has unsafe permissions: {path}")
    if created:
        _fsync_directory(path.parent)


def validate_private_directory(path: Path) -> None:
    path = Path(path)
    try:
        info = path.lstat()
    except OSError as error:
        raise OSError(f"private state directory is unavailable: {path}") from error
    if _unsupported_path(info) or not stat.S_ISDIR(info.st_mode):
        raise OSError(f"private state directory is unsafe: {path}")
    if os.name == "nt":
        if not is_private_path(path):
            raise OSError(f"private state directory is unsafe: {path}")
    elif not _posix_owned(info) or stat.S_IMODE(info.st_mode) != 0o700:
        raise OSError(f"private state directory is unsafe: {path}")


def validate_private_file(path: Path, max_bytes: int) -> None:
    path = Path(path)
    try:
        info = path.lstat()
    except OSError as error:
        raise OSError(f"private state file is unavailable: {path}") from error
    if _unsupported_path(info) or not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
        raise OSError(f"private state file is unsafe: {path}")
    if os.name == "nt":
        if not is_private_path(path):
            raise OSError(f"private state file is unsafe: {path}")
    elif not _posix_owned(info) or stat.S_IMODE(info.st_mode) != 0o600:
        raise OSError(f"private state file is unsafe: {path}")


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class PrivateStateFile:
    """Locked, bounded private file with platform-native ownership enforcement."""

    def __init__(self, path: Path, *, lock_name: str | None = None, repair_root: bool = True) -> None:
        self.path = Path(path)
        self.root = self.path.parent
        self.lock_path = self.root / (lock_name or f".{self.path.name}.lock")
        self.repair_root = repair_root
        self._directory_fd: int | None = None
        self._locked = False

    @contextmanager
    def locked(self, *, blocking: bool = True):
        ensure_private_directory(self.root, repair=self.repair_root)
        if os.name == "nt":
            self._prepare_windows_lock()
            with named_mutex(self.lock_path, blocking=blocking) as acquired:
                if not acquired:
                    yield None
                    return
                self._locked = True
                try:
                    yield self
                finally:
                    self._locked = False
            return

        directory_fd = os.open(self.root, _DIRECTORY_FLAGS)
        try:
            root_info = os.fstat(directory_fd)
            if not stat.S_ISDIR(root_info.st_mode) or not _posix_owned(root_info):
                raise OSError("private state root is unsafe")
            flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
            # APFS can return ENOENT for concurrent non-exclusive O_CREAT opens.
            # Elect one creator, then open the persistent lock without creation.
            try:
                descriptor = os.open(self.lock_path.name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory_fd)
            except FileExistsError:
                descriptor = os.open(self.lock_path.name, flags, dir_fd=directory_fd)
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or not _posix_owned(info):
                    raise OSError("private state lock is unsafe")
                os.fchmod(descriptor, 0o600)
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
                except BlockingIOError:
                    yield None
                    return
                self._directory_fd = directory_fd
                self._locked = True
                try:
                    yield self
                finally:
                    self._locked = False
                    self._directory_fd = None
            finally:
                os.close(descriptor)
        finally:
            os.close(directory_fd)

    def _prepare_windows_lock(self) -> None:
        flags = os.O_RDWR | getattr(os, "O_BINARY", 0)
        created = False
        try:
            descriptor = os.open(self.lock_path, flags | os.O_CREAT | os.O_EXCL, 0o600)
            created = True
        except FileExistsError:
            descriptor = os.open(self.lock_path, flags)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise OSError("private state lock is not a regular file")
            if created:
                secure_created_path(self.lock_path)
            else:
                secure_path(self.lock_path)
        finally:
            os.close(descriptor)

    def read(self, max_bytes: int) -> bytes | None:
        self._require_lock()
        if os.name == "nt":
            return self._read_windows(max_bytes)
        try:
            descriptor = os.open(self.path.name, _FILE_READ_FLAGS, dir_fd=self._directory_fd)
        except FileNotFoundError:
            return None
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or not _posix_owned(info):
                raise OSError("private state path is not a safe regular file")
            os.fchmod(descriptor, 0o600)
            return os.read(descriptor, max_bytes + 1)
        finally:
            os.close(descriptor)

    def _read_windows(self, max_bytes: int) -> bytes | None:
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return None
        if _unsupported_path(info) or not stat.S_ISREG(info.st_mode):
            raise OSError("private state path is not a safe regular file")
        if not owned_by_current_user(self.path):
            raise PermissionError("private state file is not owned by the current user")
        secure_path(self.path)
        descriptor = os.open(self.path, _FILE_READ_FLAGS)
        try:
            current = os.fstat(descriptor)
            if not stat.S_ISREG(current.st_mode):
                raise OSError("private state path changed during read")
            return os.read(descriptor, max_bytes + 1)
        finally:
            os.close(descriptor)

    def size(self, max_bytes: int) -> int | None:
        self._require_lock()
        if os.name == "nt":
            try:
                info = self.path.lstat()
            except FileNotFoundError:
                return None
            if _unsupported_path(info) or not stat.S_ISREG(info.st_mode):
                raise OSError("private state path is not a safe regular file")
            if not owned_by_current_user(self.path) or not is_private_path(self.path):
                raise OSError("private state file is unsafe")
            if info.st_size > max_bytes:
                raise OSError("private state file is too large")
            return info.st_size
        try:
            descriptor = os.open(self.path.name, _FILE_READ_FLAGS, dir_fd=self._directory_fd)
        except FileNotFoundError:
            return None
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or not _posix_owned(info) or stat.S_IMODE(info.st_mode) != 0o600:
                raise OSError("private state file is unsafe")
            if info.st_size > max_bytes:
                raise OSError("private state file is too large")
            return info.st_size
        finally:
            os.close(descriptor)

    def cleanup_temporaries(self) -> int:
        """Remove only atomic remnants for this target while its lock is held."""
        self._require_lock()
        pattern = re.compile(rf"\.{re.escape(self.path.name)}\.[0-9a-f]{{32}}\.tmp")
        removed = 0
        entries = os.scandir(self.root if os.name == "nt" else self._directory_fd)
        matched = 0
        with entries:
            for entry in entries:
                name = entry.name
                if pattern.fullmatch(name) is None:
                    continue
                if matched >= _TEMPORARY_CLEANUP_BATCH:
                    break
                matched += 1
                if os.name == "nt":
                    candidate = self.root / name
                    info = candidate.lstat()
                    if (
                        _unsupported_path(info)
                        or not stat.S_ISREG(info.st_mode)
                        or not owned_by_current_user(candidate)
                        or not is_private_path(candidate)
                    ):
                        raise OSError("atomic temporary file is unsafe")
                    candidate.unlink()
                else:
                    info = os.stat(name, dir_fd=self._directory_fd, follow_symlinks=False)
                    if not stat.S_ISREG(info.st_mode) or not _posix_owned(info) or stat.S_IMODE(info.st_mode) != 0o600:
                        raise OSError("atomic temporary file is unsafe")
                    os.unlink(name, dir_fd=self._directory_fd)
                removed += 1
        if removed and os.name != "nt":
            os.fsync(self._directory_fd)
        return removed

    def replace(self, payload: bytes, *, overwrite: bool = True) -> None:
        self._require_lock()
        if os.name != "nt":
            replace_bytes_at(self._directory_fd, self.path.name, payload, overwrite=overwrite)
            return
        descriptor = -1
        temporary: Path | None = None
        try:
            for _ in range(_TEMPORARY_ATTEMPTS):
                candidate = self.root / f".{self.path.name}.{secrets.token_hex(16)}.tmp"
                try:
                    descriptor = os.open(
                        candidate,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                        0o600,
                    )
                except FileExistsError:
                    continue
                temporary = candidate
                break
            if temporary is None:
                raise OSError("could not allocate an atomic temporary file")
            secure_created_path(temporary)
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            # Windows rename refuses an existing destination; replace opts in
            # to overwriting durable stores, but enrollment exports must not.
            publish = os.replace if overwrite else os.rename
            publish(temporary, self.path)
            temporary = None
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def delete(self) -> bool:
        self._require_lock()
        try:
            if os.name == "nt":
                info = self.path.lstat()
                if (
                    _unsupported_path(info)
                    or not stat.S_ISREG(info.st_mode)
                    or not owned_by_current_user(self.path)
                    or not is_private_path(self.path)
                ):
                    raise OSError("private state file is unsafe")
                self.path.unlink()
            else:
                info = os.stat(self.path.name, dir_fd=self._directory_fd, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode) or not _posix_owned(info) or stat.S_IMODE(info.st_mode) != 0o600:
                    raise OSError("private state file is unsafe")
                os.unlink(self.path.name, dir_fd=self._directory_fd)
                os.fsync(self._directory_fd)
        except FileNotFoundError:
            return False
        return True

    def quarantine(self, base_name: str) -> Path:
        self._require_lock()
        name = base_name
        suffix = 0
        while self._entry_exists(name):
            suffix += 1
            name = f"{base_name}.{suffix}"
        if os.name == "nt":
            quarantine = self.root / name
            os.replace(self.path, quarantine)
            secure_path(quarantine)
            return quarantine
        os.replace(self.path.name, name, src_dir_fd=self._directory_fd, dst_dir_fd=self._directory_fd)
        descriptor = os.open(name, _FILE_READ_FLAGS, dir_fd=self._directory_fd)
        try:
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
        os.fsync(self._directory_fd)
        return self.root / name

    def _entry_exists(self, name: str) -> bool:
        try:
            if os.name == "nt":
                (self.root / name).lstat()
            else:
                os.stat(name, dir_fd=self._directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        return True

    def _require_lock(self) -> None:
        if not self._locked:
            raise RuntimeError("private state operation requires its lock")
