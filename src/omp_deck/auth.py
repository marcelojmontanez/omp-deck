"""Persistent pairing and device authentication for OMP Deck.

The module is deliberately independent of any HTTP framework.  Callers pass the
request boundary values to :class:`AuthManager` and translate ``AuthError``
subclasses into their HTTP response policy.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from . import _auth_state
from ._private_state import PrivateStateFile, validate_private_directory, validate_private_file

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
        pairing_ttl: int = 60,
        session_ttl: int = 90 * 24 * 60 * 60,
        pairing_attempts: int = 6,
        pairing_window: float = 60,
        max_rate_limit_sources: int = 1024,
        max_devices: int = 64,
        now: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        if pairing_ttl < 1 or session_ttl < 1 or max_devices < 1 or max_devices > _auth_state.MAX_PERSISTED_DEVICES:
            raise ValueError("authentication lifetimes and limits must be positive and bounded")
        self.state_root = Path(state_root)
        self.path = self.state_root / "auth.json"
        self.pairing_ttl = pairing_ttl
        self.session_ttl = session_ttl
        self.max_devices = max_devices
        self._now = now
        self._lock = threading.RLock()
        self._storage = PrivateStateFile(self.path, lock_name=".auth.lock", repair_root=os.name == "nt")
        self._limiter = _AttemptLimiter(pairing_attempts, pairing_window, max_rate_limit_sources, monotonic)
        with self._locked_state():
            pass

    def begin_pairing(self, *, ttl: int | None = None) -> PairingSecret:
        """Create a new one-time pairing secret, invalidating any previous one."""
        lifetime = self.pairing_ttl if ttl is None else ttl
        if lifetime < 1 or lifetime > self.pairing_ttl:
            raise ValueError("pairing lifetime exceeds configured limit")
        token = f"{secrets.randbelow(1_000_000):06d}"
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
                if isinstance(pairing, dict):
                    self._state["pairing"] = None
                    self._write_state()
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

    @contextmanager
    def _locked_state(self):
        with self._lock:
            try:
                with self._storage.locked():
                    self._state = self._load_or_create()
                    yield
            except AuthError:
                raise
            except OSError as error:
                detail = str(error)
                if "root" in detail:
                    message = "authentication state root is unsafe"
                elif "lock" in detail:
                    message = "authentication lock is unsafe"
                else:
                    message = "authentication state is unavailable or unsafe"
                raise AuthError(message) from error

    def _new_state(self) -> dict:
        return {"version": STATE_VERSION, "epoch": secrets.token_hex(32), "pairing": None, "devices": {}}

    def _load_or_create(self) -> dict:
        try:
            raw = self._storage.read(_auth_state.MAX_STATE_BYTES)
            if raw is None:
                state = self._new_state()
                self._state = state
                self._write_state()
                return state
            state = json.loads(raw)
            self._validate_state(state)
            return state
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            RecursionError,
            ValueError,
            TypeError,
            KeyError,
        ) as error:
            quarantine = self.path.with_name(f"auth.json.corrupt-{int(self._now())}-{secrets.token_hex(4)}")
            try:
                quarantine = self._storage.quarantine(quarantine.name)
            except OSError:
                quarantine = self.path
            raise AuthStateCorrupt(quarantine) from error

    _validate_state = staticmethod(_auth_state.validate)

    def _prune_expired(self, now: int) -> None:
        self._state["devices"] = {
            key: value
            for key, value in self._state["devices"].items()
            if value["expiresAt"] >= now and value["epoch"] == self._state["epoch"]
        }

    def _write_state(self) -> None:
        data = (json.dumps(self._state, sort_keys=True, separators=(",", ":")) + "\n").encode()
        self._storage.replace(data)


def validate_auth_state(state_root: str | os.PathLike[str]) -> None:
    root = Path(state_root)
    path = root / "auth.json"
    if not root.exists() and not root.is_symlink():
        raise RuntimeError("authentication state is missing")
    try:
        validate_private_directory(root)
    except OSError as error:
        raise RuntimeError("authentication state directory is unsafe") from error
    try:
        validate_private_file(path, _auth_state.MAX_STATE_BYTES)
    except OSError as error:
        raise RuntimeError("authentication state file is unsafe") from error
    lock = root / ".auth.lock"
    if lock.exists() or lock.is_symlink():
        try:
            validate_private_file(lock, _auth_state.MAX_STATE_BYTES)
        except OSError as error:
            raise RuntimeError("authentication lock is unsafe") from error
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        AuthManager._validate_state(state)
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        RecursionError,
        ValueError,
        TypeError,
        KeyError,
    ) as error:
        raise RuntimeError("authentication state is invalid") from error
