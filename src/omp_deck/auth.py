"""Persistent pairing and device authentication for OMP Deck.

The module is deliberately independent of any HTTP framework.  Callers pass the
request boundary values to :class:`AuthManager` and translate ``AuthError``
subclasses into their HTTP response policy.
"""

from __future__ import annotations
import fcntl

import hashlib
import hmac
import json
import os
import secrets
import stat
import threading
import time
from contextlib import contextmanager
from collections import OrderedDict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

STATE_VERSION = 1
COOKIE_NAME = "omp_deck_device"
COOKIE_ATTRIBUTES = "Path=/; HttpOnly; Secure; SameSite=Strict"


class AuthError(Exception):
    """Base class for safe, non-secret authentication failures."""


class AuthenticationFailed(AuthError):
    pass


class PairingFailed(AuthError):
    pass


class PairingRateLimited(PairingFailed):
    pass


class AuthStateCorrupt(AuthError):
    """Raised after an invalid state file has been moved aside."""

    def __init__(self, quarantine_path: Path):
        super().__init__("authentication state is corrupt and was quarantined")
        self.quarantine_path = quarantine_path


@dataclass(frozen=True)
class PairingSecret:
    token: str
    expires_at: int


@dataclass(frozen=True)
class AuthenticatedDevice:
    device_id: str
    csrf_token: str
    created_at: int


@dataclass(frozen=True)
class PairedDevice:
    device: AuthenticatedDevice
    credential: str
    set_cookie: str


class _AttemptLimiter:
    def __init__(self, limit: int, window: float, max_sources: int, clock: Callable[[], float]):
        if limit < 1 or window <= 0 or max_sources < 1:
            raise ValueError("invalid pairing rate-limit configuration")
        self.limit = limit
        self.window = window
        self.max_sources = max_sources
        self.clock = clock
        self.sources: OrderedDict[str, deque[float]] = OrderedDict()

    def consume(self, source: str) -> bool:
        now = self.clock()
        attempts = self.sources.pop(source, deque())
        cutoff = now - self.window
        while attempts and attempts[0] <= cutoff:
            attempts.popleft()
        allowed = len(attempts) < self.limit
        attempts.append(now)
        # Keep even rejected attempts, but cap memory and per-source history.
        while len(attempts) > self.limit:
            attempts.popleft()
        self.sources[source] = attempts
        while len(self.sources) > self.max_sources:
            self.sources.popitem(last=False)
        return allowed


class AuthManager:
    """Owns pairing, durable device sessions, revocation, and CSRF tokens."""

    def __init__(
        self,
        state_root: str | os.PathLike[str],
        *,
        pairing_ttl: int = 300,
        session_ttl: int = 90 * 24 * 60 * 60,
        pairing_attempts: int = 6,
        pairing_window: float = 60,
        max_rate_limit_sources: int = 1024,
        max_devices: int = 64,
        now: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        if pairing_ttl < 1 or session_ttl < 1 or max_devices < 1:
            raise ValueError("authentication lifetimes and limits must be positive")
        self.state_root = Path(state_root)
        self.path = self.state_root / "auth.json"
        self.pairing_ttl = pairing_ttl
        self.session_ttl = session_ttl
        self.max_devices = max_devices
        self._now = now
        self._lock = threading.RLock()
        self._directory_fd = None
        self._limiter = _AttemptLimiter(
            pairing_attempts, pairing_window, max_rate_limit_sources, monotonic
        )
        with self._locked_state():
            pass

    def begin_pairing(self, *, ttl: int | None = None) -> PairingSecret:
        """Create a new one-time pairing secret, invalidating any previous one."""
        lifetime = self.pairing_ttl if ttl is None else ttl
        if lifetime < 1 or lifetime > self.pairing_ttl:
            raise ValueError("pairing lifetime exceeds configured limit")
        token = secrets.token_urlsafe(32)
        expires = int(self._now()) + lifetime
        with self._locked_state():
            self._state["pairing"] = {
                "verifier": self._verifier(token),
                "expiresAt": expires,
            }
            self._write_state()
        return PairingSecret(token, expires)

    def pair(self, token: str, *, source: str) -> PairedDevice:
        """Consume a pairing secret and issue a new opaque device credential."""
        if not source or len(source) > 256:
            raise PairingFailed("invalid pairing source")
        with self._locked_state():
            if not self._limiter.consume(source):
                raise PairingRateLimited("too many pairing attempts")
            pairing = self._state.get("pairing")
            candidate = self._verifier(token) if isinstance(token, str) else ""
            valid = (
                isinstance(pairing, dict)
                and int(pairing.get("expiresAt", 0)) >= int(self._now())
                and hmac.compare_digest(str(pairing.get("verifier", "")), candidate)
            )
            if not valid:
                raise PairingFailed("invalid or expired pairing secret")

            credential = "v1." + secrets.token_urlsafe(32)
            device_id = secrets.token_hex(16)
            created = int(self._now())
            expires = created + self.session_ttl
            self._state["pairing"] = None  # consume before the durable commit
            self._state["devices"][device_id] = {
                "verifier": self._verifier(credential),
                "createdAt": created,
                "expiresAt": expires,
                "epoch": self._state["epoch"],
            }
            self._prune_expired(created)
            while len(self._state["devices"]) > self.max_devices:
                oldest = min(
                    self._state["devices"],
                    key=lambda key: self._state["devices"][key]["createdAt"],
                )
                del self._state["devices"][oldest]
            self._write_state()
            device = AuthenticatedDevice(
                device_id=device_id,
                csrf_token=self._csrf(credential, device_id, self._state["epoch"]),
                created_at=created,
            )
            return PairedDevice(device, credential, self.cookie_header(credential))

    def authenticate(self, credential: str | None) -> AuthenticatedDevice:
        """Validate an opaque cookie credential using timing-safe comparison."""
        with self._locked_state():
            return self._authenticate_current(credential)

    def validate_csrf(self, credential: str | None, csrf_token: str | None) -> AuthenticatedDevice:
        device = self.authenticate(credential)
        supplied = csrf_token if isinstance(csrf_token, str) else ""
        if not hmac.compare_digest(device.csrf_token, supplied):
            raise AuthenticationFailed("invalid CSRF token")
        return device

    def logout(self, credential: str | None) -> str:
        """Revoke one device and return an expiring Set-Cookie value."""
        with self._locked_state():
            device = self._authenticate_current(credential)
            del self._state["devices"][device.device_id]
            self._write_state()
        return self.clear_cookie_header()

    def revoke_all(self, credential: str | None, csrf_token: str | None) -> str:
        """Invalidate every credential by rotating the installation epoch."""
        with self._locked_state():
            device = self._authenticate_current(credential)
            supplied = csrf_token if isinstance(csrf_token, str) else ""
            if not hmac.compare_digest(device.csrf_token, supplied):
                raise AuthenticationFailed("invalid CSRF token")
            self._state["epoch"] = secrets.token_hex(32)
            self._state["devices"] = {}
            self._state["pairing"] = None
            self._write_state()
        return self.clear_cookie_header()

    @staticmethod
    def cookie_header(credential: str) -> str:
        return f"{COOKIE_NAME}={credential}; {COOKIE_ATTRIBUTES}"

    @staticmethod
    def clear_cookie_header() -> str:
        return f"{COOKIE_NAME}=; Max-Age=0; {COOKIE_ATTRIBUTES}"

    @staticmethod
    def _verifier(secret: str | None) -> str:
        if not secret:
            return hashlib.sha256(b"invalid").hexdigest()
        return hashlib.sha256(secret.encode("utf-8")).hexdigest()

    @staticmethod
    def _csrf(credential: str, device_id: str, epoch: str) -> str:
        message = f"omp-deck-csrf-v1\0{device_id}\0{epoch}".encode()
        return hmac.new(credential.encode(), message, hashlib.sha256).hexdigest()

    def _authenticate_current(self, credential: str | None) -> AuthenticatedDevice:
        candidate = self._verifier(credential) if isinstance(credential, str) else ""
        now = int(self._now())
        matched_id = None
        matched_record = None
        for device_id, record in self._state["devices"].items():
            if hmac.compare_digest(str(record["verifier"]), candidate):
                matched_id, matched_record = device_id, record
        if (
            matched_record is None
            or matched_record["epoch"] != self._state["epoch"]
            or matched_record["expiresAt"] < now
        ):
            raise AuthenticationFailed("invalid or expired device credential")
        return AuthenticatedDevice(
            device_id=matched_id,
            csrf_token=self._csrf(credential, matched_id, self._state["epoch"]),
            created_at=matched_record["createdAt"],
        )

    def _ensure_state_root(self) -> None:
        try:
            self.state_root.mkdir(parents=True, mode=0o700)
        except FileExistsError:
            pass
        try:
            info = self.state_root.lstat()
        except OSError as error:
            raise AuthError("authentication state root is unavailable") from error
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
        ):
            raise AuthError("authentication state root is unsafe")

    @contextmanager
    def _locked_state(self):
        with self._lock:
            self._ensure_state_root()
            directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
            try:
                directory_fd = os.open(self.state_root, directory_flags)
            except OSError as error:
                raise AuthError("authentication state root is unavailable") from error
            try:
                root_info = os.fstat(directory_fd)
                if (
                    not stat.S_ISDIR(root_info.st_mode)
                    or root_info.st_uid != os.geteuid()
                    or root_info.st_mode & 0o077
                ):
                    raise AuthError("authentication state root is unsafe")
                try:
                    lock_fd = os.open(
                        ".auth.lock",
                        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                        0o600,
                        dir_fd=directory_fd,
                    )
                except OSError as error:
                    raise AuthError("authentication lock is unavailable") from error
                try:
                    info = os.fstat(lock_fd)
                    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
                        raise AuthError("authentication lock is unsafe")
                    os.fchmod(lock_fd, 0o600)
                    fcntl.flock(lock_fd, fcntl.LOCK_EX)
                    self._directory_fd = directory_fd
                    self._state = self._load_or_create(directory_fd)
                    yield
                finally:
                    self._directory_fd = None
                    os.close(lock_fd)
            finally:
                os.close(directory_fd)

    def _new_state(self) -> dict:
        return {"version": STATE_VERSION, "epoch": secrets.token_hex(32), "pairing": None, "devices": {}}

    def _load_or_create(self, directory_fd) -> dict:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open("auth.json", flags, dir_fd=directory_fd)
        except FileNotFoundError:
            state = self._new_state()
            self._state = state
            self._write_state()
            return state
        except OSError as error:
            raise AuthStateCorrupt(self.path) from error
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
                raise AuthStateCorrupt(self.path)
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
                descriptor = -1
                state = json.load(stream)
            self._validate_state(state)
            return state
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError, KeyError):
            quarantine = self.path.with_name(
                f"auth.json.corrupt-{int(self._now())}-{secrets.token_hex(4)}"
            )
            try:
                os.replace(
                    "auth.json",
                    quarantine.name,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                )
                os.fsync(directory_fd)
            except OSError:
                quarantine = self.path
            raise AuthStateCorrupt(quarantine)
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    @staticmethod
    def _validate_state(state: object) -> None:
        if not isinstance(state, dict) or set(state) != {"version", "epoch", "pairing", "devices"}:
            raise ValueError("invalid auth state shape")
        if state["version"] != STATE_VERSION or not isinstance(state["epoch"], str):
            raise ValueError("unsupported auth state")
        if not isinstance(state["devices"], dict):
            raise ValueError("invalid devices")
        pairing = state["pairing"]
        if pairing is not None and (
            not isinstance(pairing, dict)
            or set(pairing) != {"verifier", "expiresAt"}
            or not isinstance(pairing["verifier"], str)
            or not isinstance(pairing["expiresAt"], int)
        ):
            raise ValueError("invalid pairing verifier")
        for device_id, record in state["devices"].items():
            if (
                not isinstance(device_id, str)
                or not isinstance(record, dict)
                or set(record) != {"verifier", "createdAt", "expiresAt", "epoch"}
                or not isinstance(record["verifier"], str)
                or not isinstance(record["createdAt"], int)
                or not isinstance(record["expiresAt"], int)
                or not isinstance(record["epoch"], str)
            ):
                raise ValueError("invalid device record")

    def _prune_expired(self, now: int) -> None:
        self._state["devices"] = {
            key: value
            for key, value in self._state["devices"].items()
            if value["expiresAt"] >= now and value["epoch"] == self._state["epoch"]
        }

    def _write_state(self) -> None:
        if self._directory_fd is None:
            raise RuntimeError("authentication state write requires its lock")
        data = (json.dumps(self._state, sort_keys=True, separators=(",", ":")) + "\n").encode()
        temporary = f".auth.json.{secrets.token_hex(8)}.tmp"
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=self._directory_fd,
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(
                temporary,
                "auth.json",
                src_dir_fd=self._directory_fd,
                dst_dir_fd=self._directory_fd,
            )
            os.fsync(self._directory_fd)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                os.unlink(temporary, dir_fd=self._directory_fd)
            except FileNotFoundError:
                pass


def validate_auth_state(state_root: str | os.PathLike[str]) -> None:
    root = Path(state_root)
    path = root / "auth.json"
    try:
        root_stat = root.lstat()
        path_stat = path.lstat()
    except FileNotFoundError as error:
        raise RuntimeError("authentication state is missing") from error
    except OSError as error:
        raise RuntimeError(f"cannot inspect authentication state: {error}") from error
    if (
        root.is_symlink()
        or not stat.S_ISDIR(root_stat.st_mode)
        or root_stat.st_uid != os.geteuid()
        or stat.S_IMODE(root_stat.st_mode) != 0o700
    ):
        raise RuntimeError("authentication state directory is unsafe")
    if (
        path.is_symlink()
        or not stat.S_ISREG(path_stat.st_mode)
        or path_stat.st_uid != os.geteuid()
        or stat.S_IMODE(path_stat.st_mode) != 0o600
    ):
        raise RuntimeError("authentication state file is unsafe")
    lock = root / ".auth.lock"
    if lock.exists() or lock.is_symlink():
        try:
            lock_stat = lock.lstat()
        except OSError as error:
            raise RuntimeError(f"cannot inspect authentication lock: {error}") from error
        if (
            lock.is_symlink()
            or not stat.S_ISREG(lock_stat.st_mode)
            or lock_stat.st_uid != os.geteuid()
            or stat.S_IMODE(lock_stat.st_mode) != 0o600
        ):
            raise RuntimeError("authentication lock is unsafe")
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        AuthManager._validate_state(state)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError, KeyError) as error:
        raise RuntimeError("authentication state is invalid") from error
