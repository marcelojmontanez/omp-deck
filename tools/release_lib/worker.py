from __future__ import annotations

import importlib
import math
import os
import re
import secrets
import subprocess
import time
from pathlib import Path

from omp_deck.auth import COOKIE_NAME, COORDINATOR_COOKIE_NAME, AuthenticationFailed, AuthManager

from .worker_state import read_json as _read_json
from .worker_state import write_json as _private_json
from .worker_verify import verify_http, verify_public, write_browser_input

_TX = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
ARM_SECONDS = 15 * 60
POLL_SECONDS = 2
ROLLBACK_RETRIES = 3


def _paths(request: dict) -> tuple[Path, Path]:
    if request.get("version") != 1 or request.get("kind") not in {"node", "coordinator"}:
        raise RuntimeError("unsupported release worker request")
    transaction = request.get("transaction")
    root_value = request.get("root")
    if not isinstance(transaction, str) or not _TX.fullmatch(transaction):
        raise RuntimeError("invalid release transaction")
    if not isinstance(root_value, str) or not Path(root_value).is_absolute():
        raise RuntimeError("invalid release root")
    root = Path(root_value)
    if root.is_symlink() or not root.is_dir() or root.stat().st_uid != os.getuid():
        raise RuntimeError("unsafe release root")
    guard = root / "guard"
    guard.mkdir(mode=0o700, exist_ok=True)
    if guard.is_symlink() or guard.stat().st_uid != os.getuid():
        raise RuntimeError("unsafe release guard directory")
    os.chmod(root, 0o700)
    os.chmod(guard, 0o700)
    return root, guard


def _native(request: dict):
    return importlib.import_module(f"{__package__}.{request['kind']}")


def _cookie_name(request: dict) -> str:
    return COORDINATOR_COOKIE_NAME if request["kind"] == "coordinator" else COOKIE_NAME


def _mint(request: dict, guard: Path) -> Path:
    state_root = Path(request["profile"]["state_root"])
    manager = AuthManager(state_root, session_ttl=ARM_SECONDS, cookie_name=_cookie_name(request))
    paired = manager.authenticate_or_issue(None, evict_oldest=False)
    path = guard / "credential.json"
    try:
        _private_json(path, {"version": 1, "credential": paired.credential, "deviceId": paired.device.device_id})
    except Exception:
        manager.logout(paired.credential)
        raise
    return path


def _credential(guard: Path) -> str:
    value = _read_json(guard / "credential.json", limit=4096)
    credential = value.get("credential")
    if value.get("version") != 1 or not isinstance(credential, str):
        raise RuntimeError("invalid maintenance credential")
    return credential


def _revoke(request: dict, guard: Path) -> None:
    path = guard / "credential.json"
    try:
        credential = _credential(guard)
    except FileNotFoundError:
        return
    manager = AuthManager(Path(request["profile"]["state_root"]), cookie_name=_cookie_name(request))
    try:
        manager.logout(credential)
    except AuthenticationFailed:
        pass
    try:
        manager.authenticate(credential)
    except AuthenticationFailed:
        pass
    else:
        raise RuntimeError("maintenance credential revocation failed")
    path.unlink(missing_ok=True)


def cleanup(request: dict) -> dict:
    root, guard = _paths(request)
    error = None
    _private_json(guard / "cleanup.json", {"version": 1, "transaction": request["transaction"]})
    try:
        _revoke(request, guard)
    except Exception as caught:
        error = caught
    for path in (
        root / "browser-input.json",
        root / "browser-result.json",
        root / "verification.json",
        root / "watchdog-ready.json",
        guard / "watchdog-request.json",
        guard / "worker-state.json",
        guard / "verification-pending.json",
    ):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    if error is not None:
        raise error
    return {"version": 1, "state": "absent"}


def arm(request: dict) -> dict:
    root, guard = _paths(request)
    marker_path = root / "watchdog-ready.json"
    cleanup_marker = guard / "cleanup.json"
    if cleanup_marker.exists():
        marker_path.unlink(missing_ok=True)
        time.sleep(POLL_SECONDS + 0.25)
        cleanup_marker.unlink(missing_ok=True)
    if marker_path.exists() and (guard / "watchdog-request.json").exists():
        marker = _read_json(marker_path, limit=4096)
        saved = _read_json(guard / "watchdog-request.json")
        if marker.get("transaction") == request["transaction"] and marker.get("nonce") == saved.get("_watchdog_nonce"):
            try:
                os.kill(int(marker["pid"]), 0)
            except (OSError, ValueError, TypeError):
                cleanup(request)
            else:
                return {"version": 1, "state": "prepared", "watchdogPid": marker["pid"]}
    cleanup_marker.unlink(missing_ok=True)
    credential_path = _mint(request, guard)
    request["cookie_file"] = str(credential_path)
    nonce = secrets.token_hex(32)
    watchdog_request = dict(request)
    watchdog_request["_watchdog_nonce"] = nonce
    request_path = guard / "watchdog-request.json"
    _private_json(request_path, watchdog_request)
    _private_json(
        guard / "worker-state.json",
        {
            "version": 1,
            "transaction": request["transaction"],
            "state": "armed",
            "armDeadline": int(time.time()) + ARM_SECONDS,
        },
    )
    prefix = request.get("_worker_argv")
    if not isinstance(prefix, list) or not prefix or not all(isinstance(item, str) and item for item in prefix):
        cleanup(request)
        raise RuntimeError("watchdog launch command unavailable")
    try:
        subprocess.Popen(
            [*prefix, "watchdog", str(request_path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            marker_path = root / "watchdog-ready.json"
            if marker_path.exists():
                marker = _read_json(marker_path, limit=4096)
                if (
                    marker.get("version") == 1
                    and marker.get("transaction") == request["transaction"]
                    and marker.get("nonce") == nonce
                    and int(marker.get("pid", 0)) > 0
                    and abs(int(time.time()) - int(marker.get("readyAt", 0))) <= 30
                ):
                    return {"version": 1, "state": "prepared", "watchdogPid": marker["pid"]}
            time.sleep(0.1)
        raise RuntimeError("release watchdog readiness timed out")
    except Exception:
        cleanup(request)
        raise


def _verify_prepare(request: dict) -> dict:
    root, guard = _paths(request)
    credential = _credential(guard)
    checks = verify_http(request, credential, _cookie_name(request))
    digest = write_browser_input(request, credential, _cookie_name(request), root / "browser-input.json")
    _private_json(
        guard / "verification-pending.json",
        {"version": 1, "transaction": request["transaction"], "inputSha256": digest, "checks": checks},
    )
    return {"version": 1, "browserInputPath": str(root / "browser-input.json")}


def _verify(request: dict) -> dict:
    root, guard = _paths(request)
    pending = _read_json(guard / "verification-pending.json", limit=4096)
    result = _read_json(root / "browser-result.json", limit=16384)
    expected_build = {"version": request["candidate"]["wheelVersion"], "revision": request["candidate"]["revision"]}
    if result != {
        "version": 1,
        "transaction": request["transaction"],
        "inputSha256": pending["inputSha256"],
        "build": expected_build,
        "ok": True,
        "checks": ["viewport", "read-only", "healthy"],
    }:
        raise RuntimeError("browser verification receipt is invalid or stale")
    checks = verify_http(request, _credential(guard), _cookie_name(request)) + ["browser-smoke"]
    verification = {
        "version": 1,
        "transaction": request["transaction"],
        "completedAt": int(time.time()),
        "checks": checks,
    }
    _private_json(root / "verification.json", verification)
    routed = dict(request)
    routed["cookie_file"] = str(guard / "credential.json")
    routed["worker_verification"] = verification
    result_value = _native(request).execute("verify", routed)
    if result_value.get("state") == "verified":
        cleanup(request)
    return result_value


def execute(action: str, request: dict) -> dict:
    if not isinstance(request, dict):
        raise RuntimeError("invalid release worker request")
    root, guard = _paths(request)
    if action == "arm":
        return arm(request)
    if action == "cleanup":
        return cleanup(request)
    if action == "verify_prepare":
        return _verify_prepare(request)
    if action == "verify":
        return _verify(request)
    if action not in {"prepare", "status", "deploy", "rollback"}:
        raise RuntimeError("unsupported release worker action")
    routed = dict(request)
    if (guard / "credential.json").exists():
        routed["cookie_file"] = str(guard / "credential.json")
    result = _native(request).execute(action, routed)
    if action == "prepare":
        verify_public(request, request["rollback"])
    if (action == "rollback" and result.get("state") == "rolledback") or (
        action == "status" and result.get("state") in {"verified", "rolledback", "rollback_failed"}
    ):
        cleanup(request)
    return result


def watchdog(request: dict) -> dict:
    root, guard = _paths(request)
    nonce = request.get("_watchdog_nonce")
    if not isinstance(nonce, str) or len(nonce) != 64:
        raise RuntimeError("invalid watchdog nonce")
    _private_json(
        root / "watchdog-ready.json",
        {
            "version": 1,
            "transaction": request["transaction"],
            "pid": os.getpid(),
            "nonce": nonce,
            "readyAt": int(time.time()),
        },
    )
    armed = _read_json(guard / "worker-state.json", limit=4096)
    unsafe_failures = 0
    while True:
        if (guard / "cleanup.json").exists():
            (root / "watchdog-ready.json").unlink(missing_ok=True)
            return {"version": 1, "state": "absent"}
        try:
            status = _native(request).execute("status", request)
            state = status.get("state")
            unsafe_failures = 0
        except Exception:
            unsafe_failures += 1
            if unsafe_failures < ROLLBACK_RETRIES:
                time.sleep(min(2 ** (unsafe_failures - 1), 4))
                continue
            status = {}
            state = "unsafe"
        if state in {"verified", "rolledback", "rollback_failed"}:
            cleanup(request)
            return {"version": 1, "state": state}
        _private_json(
            root / "watchdog-ready.json",
            {
                "version": 1,
                "transaction": request["transaction"],
                "pid": os.getpid(),
                "nonce": nonce,
                "readyAt": int(time.time()),
            },
        )
        now = time.time()
        deadline = status.get("deadline") or status.get("rollbackDeadline")
        if (guard / "cleanup.json").exists():
            (root / "watchdog-ready.json").unlink(missing_ok=True)
            return {"version": 1, "state": "absent"}
        numeric_deadline = (
            isinstance(deadline, (int, float)) and not isinstance(deadline, bool) and math.isfinite(deadline)
        )
        expired = state in {"pending", "unsafe"} and (not numeric_deadline or now >= deadline)
        abandoned = state in {"absent", "prepared"} and now >= armed["armDeadline"]
        if expired:
            for attempt in range(ROLLBACK_RETRIES):
                try:
                    result = _native(request).execute("rollback", request)
                    if result.get("state") == "rolledback":
                        cleanup(request)
                        return {"version": 1, "state": "rolledback"}
                except Exception:
                    pass
                time.sleep(min(2**attempt, 4))
            _private_json(
                guard / "worker-state.json",
                {
                    "version": 1,
                    "transaction": request["transaction"],
                    "state": "rollback_failed",
                    "failedAt": int(time.time()),
                },
            )
            _revoke(request, guard)
            (root / "watchdog-ready.json").unlink(missing_ok=True)
            return {"version": 1, "state": "rollback_failed"}
        if abandoned:
            cleanup(request)
            return {"version": 1, "state": "absent"}
        time.sleep(POLL_SECONDS)
