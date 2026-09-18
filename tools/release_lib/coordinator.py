from __future__ import annotations

import contextlib
import hashlib
import json
import os
import platform
import re
import shlex
import stat
import subprocess
import tempfile
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from . import node_common
from .node_artifacts import inspect_wheel

SCHEMA = 1
WINDOW_SECONDS = 300
_TX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
_SHA = re.compile(r"^[a-f0-9]{64}$")
_REV = re.compile(r"^[a-f0-9]{40}$")


class CoordinatorError(RuntimeError):
    pass


def _path(value: object, label: str) -> Path:
    if not isinstance(value, str) or not value or not os.path.isabs(value):
        raise CoordinatorError(f"{label} must be an absolute path")
    path = Path(value)
    if ".." in path.parts:
        raise CoordinatorError(f"{label} contains traversal")
    return path


def _profile(request: dict[str, Any]) -> dict[str, Any]:
    value = request.get("profile")
    required = {
        "platform",
        "user",
        "home",
        "uv",
        "base_python",
        "omp",
        "tool_root",
        "entrypoint",
        "config",
        "unit",
        "state_root",
        "plugin_expected",
        "service_name",
        "rollback_root",
        "port",
        "launch_domain",
        "launch_label",
        "releases_root",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise CoordinatorError("coordinator profile is incomplete")
    result = dict(value)
    path_fields = required - {"platform", "user", "service_name", "port", "launch_domain", "launch_label"}
    for key in path_fields:
        result[key] = _path(result[key], f"profile.{key}")
    if result["platform"] != "linux" or platform.system().lower() != "linux":
        raise CoordinatorError("coordinator release requires Linux")
    if result["user"] != __import__("pwd").getpwuid(os.geteuid()).pw_name or result["home"] != Path.home():
        raise CoordinatorError("coordinator release user or home mismatch")
    return result


def _context(request: dict[str, Any]) -> tuple[dict[str, Any], Path, Path]:
    if request.get("version") != 1 or request.get("kind") != "coordinator":
        raise CoordinatorError("unsupported coordinator request")
    tx = request.get("transaction")
    if not isinstance(tx, str) or _TX.fullmatch(tx) is None:
        raise CoordinatorError("invalid transaction identifier")
    profile = _profile(request)
    root = _path(request.get("root"), "root")
    if root != profile["rollback_root"] / tx:
        raise CoordinatorError("transaction root is outside the rollback root")
    if root.is_symlink() or not root.is_dir() or root.stat(follow_symlinks=False).st_uid != os.geteuid():
        raise CoordinatorError("unsafe transaction root")
    guard = root / "guard"
    guard.mkdir(mode=0o700, exist_ok=True)
    if guard.is_symlink() or stat.S_IMODE(guard.stat(follow_symlinks=False).st_mode) != 0o700:
        raise CoordinatorError("unsafe coordinator guard directory")
    return profile, root, guard


def _read(path: Path, *, private: bool = False, maximum: int = 128 * 1024 * 1024) -> bytes:
    try:
        return node_common.read_file(path, maximum=maximum, private=private)
    except node_common.ReleaseError as error:
        raise CoordinatorError(str(error)) from error


def _atomic(path: Path, data: bytes, mode: int = 0o600) -> None:
    if mode == 0o600:
        try:
            node_common.atomic_write(path, data)
            return
        except node_common.ReleaseError as error:
            raise CoordinatorError(str(error)) from error
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        dfd = os.open(path.parent, os.O_RDONLY)
        os.fsync(dfd)
        os.close(dfd)
    finally:
        if fd >= 0:
            os.close(fd)
        temporary.unlink(missing_ok=True)


def _save(path: Path, value: dict[str, Any]) -> None:
    _atomic(path, (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode())


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(_read(path, private=True, maximum=1024 * 1024))
    except (OSError, ValueError) as error:
        raise CoordinatorError("invalid coordinator release state") from error
    if not isinstance(value, dict) or value.get("version") != SCHEMA:
        raise CoordinatorError("unsupported coordinator release state")
    return value


@contextlib.contextmanager
def _lock(path: Path) -> Iterator[None]:
    try:
        with node_common.lock(path):
            yield
    except node_common.ReleaseError as error:
        raise CoordinatorError(str(error)) from error


def _run(argv: list[object], profile: dict[str, Any], timeout: int = 300) -> bytes:
    runtime_profile = type("RuntimeProfile", (), {"home": profile["home"]})()
    environment = {"UV_PYTHON_DOWNLOADS": "never"}
    try:
        return node_common.run(runtime_profile, argv, timeout=timeout, env=environment).stdout
    except node_common.ReleaseError as error:
        raise CoordinatorError(str(error)) from error


def _hash(path: Path, *, private: bool = False) -> str:
    return hashlib.sha256(_read(path, private=private)).hexdigest()


def _native_profile(profile: dict[str, Any]) -> node_common.Profile:
    fields = {
        name: profile[name]
        for name in (
            "platform",
            "user",
            "home",
            "uv",
            "base_python",
            "omp",
            "tool_root",
            "entrypoint",
            "config",
            "unit",
            "state_root",
            "plugin_expected",
            "service_name",
            "rollback_root",
            "port",
            "launch_domain",
            "launch_label",
        )
    }
    return node_common.Profile(**fields)


def _active(profile: dict[str, Any]) -> bool:
    try:
        return node_common.running(_native_profile(profile))
    except node_common.ReleaseError as error:
        raise CoordinatorError(str(error)) from error


def _wheel(artifact: object) -> tuple[dict[str, str], Path]:
    if not isinstance(artifact, dict):
        raise CoordinatorError("invalid artifact")
    path = _path(artifact.get("wheelPath"), "wheelPath")
    sha, version, revision = artifact.get("wheelSha256"), artifact.get("wheelVersion"), artifact.get("revision")
    if (
        not isinstance(sha, str)
        or _SHA.fullmatch(sha) is None
        or not isinstance(revision, str)
        or _REV.fullmatch(revision) is None
        or not isinstance(version, str)
    ):
        raise CoordinatorError("invalid artifact identity")
    try:
        actual_sha, actual_version, _ = inspect_wheel(path)
    except node_common.ReleaseError as error:
        raise CoordinatorError(str(error)) from error
    if actual_sha != sha:
        raise CoordinatorError("wheel hash mismatch")
    if actual_version != version or revision not in version:
        raise CoordinatorError("wheel identity mismatch")
    return {"wheelPath": str(path), "wheelSha256": sha, "wheelVersion": version, "revision": revision}, path


def _enabled(profile: dict[str, Any]) -> bool:
    result = subprocess.run(
        ["systemctl", "--user", "is-enabled", profile["service_name"]],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if result.returncode not in (0, 1):
        raise CoordinatorError("cannot determine service policy")
    return result.returncode == 0


def _identity(env: Path, profile: dict[str, Any]) -> dict[str, str]:
    code = "import json; from omp_deck import build_identity; print(json.dumps(build_identity(), sort_keys=True))"
    try:
        value = json.loads(_run([env / "bin/python", "-c", code], profile, 30))
    except (UnicodeError, ValueError) as error:
        raise CoordinatorError("installed coordinator identity is invalid") from error
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "revision"}
        or not isinstance(value["version"], str)
        or not isinstance(value["revision"], str)
        or _REV.fullmatch(value["revision"]) is None
    ):
        raise CoordinatorError("installed coordinator identity is invalid")
    return value


def _unit_commands(payload: bytes) -> list[tuple[int, bytes, list[str]]]:
    result = []
    for index, line in enumerate(payload.splitlines(keepends=True)):
        plain = line.rstrip(b"\r\n")
        if plain.startswith(b"ExecStart="):
            try:
                parsed = shlex.split(plain[len(b"ExecStart=") :].decode())
            except (UnicodeError, ValueError) as error:
                raise CoordinatorError("invalid coordinator ExecStart") from error
            result.append((index, line, parsed))
    return result


def _guard(state: dict[str, Any], profile: dict[str, Any]) -> None:
    if _hash(profile["config"], private=True) != state["configSha256"]:
        raise CoordinatorError("coordinator configuration changed")
    fleet = profile["state_root"] / "fleet-coordinator.json"
    if _hash(fleet, private=True) != state["fleetSha256"]:
        raise CoordinatorError("coordinator fleet trust changed")
    if _enabled(profile) != state["serviceEnabled"]:
        raise CoordinatorError("coordinator enabled policy changed")


def _result(state: str, installed: dict[str, str] | None = None, deadline: float | None = None) -> dict[str, Any]:
    value: dict[str, Any] = {"version": 1, "state": state, "pluginActivation": "unchanged"}
    if installed is not None:
        value["installed"] = installed
    if state == "pending":
        value["deadline"] = deadline
    return value


def _live_identity(profile: dict[str, Any]) -> dict[str, str]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{profile['port']}/api/client-config",
        headers={"Host": f"127.0.0.1:{profile['port']}"},
    )
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=10) as response:
            value = json.loads(response.read(64 * 1024))
            status = response.status
    except Exception as error:
        raise CoordinatorError("running coordinator identity check failed") from error
    build = value.get("build") if isinstance(value, dict) else None
    if (
        status != 200
        or not isinstance(value, dict)
        or value.get("mode") != "coordinator"
        or not isinstance(build, dict)
        or set(build) != {"version", "revision"}
        or not isinstance(build["version"], str)
        or not isinstance(build["revision"], str)
        or _REV.fullmatch(build["revision"]) is None
    ):
        raise CoordinatorError("running coordinator identity is invalid")
    return build


def execute(action: str, request: dict[str, Any]) -> dict[str, Any]:
    profile, root, guard = _context(request)
    manifest, lock = guard / "coordinator-state.json", guard / "coordinator.lock"
    with _lock(lock):
        if action == "prepare":
            from .coordinator_prepare import prepare

            return prepare(request, profile, root, manifest)
        if not manifest.exists():
            if action == "status":
                return _result("absent")
            raise CoordinatorError("coordinator release is not prepared")
        state = _load(manifest)
        if action == "status":
            unit_hash = _hash(profile["unit"])
            if unit_hash not in {state["newUnitSha256"], state["oldUnitSha256"]}:
                raise CoordinatorError("coordinator unit has unrelated changes")
            if not _active(profile):
                raise CoordinatorError("coordinator service is not running")
            _health(profile)
            installed = _live_identity(profile)
            return _result(state["decision"], installed, state.get("deadline"))
        if action == "deploy":
            return _deploy(request, profile, root, state, manifest)
        if action == "verify":
            return _verify(profile, root, state, manifest)
        if action == "rollback":
            return _rollback(profile, state, manifest)
        raise CoordinatorError("unknown coordinator action")


def _readiness(path: Path, state: dict[str, Any]) -> None:
    try:
        value = json.loads(_read(path, private=True, maximum=64 * 1024))
    except (OSError, ValueError) as error:
        raise CoordinatorError("watchdog readiness is unavailable") from error
    if not isinstance(value, dict) or set(value) != {"version", "transaction", "pid", "nonce", "readyAt"}:
        raise CoordinatorError("watchdog readiness has an unexpected shape")
    pid, nonce, ready_at = value.get("pid"), value.get("nonce"), value.get("readyAt")
    if (
        value.get("version") != 1
        or value.get("transaction") != state["transaction"]
        or not isinstance(pid, int)
        or isinstance(pid, bool)
        or pid <= 0
        or not isinstance(nonce, str)
        or _SHA.fullmatch(nonce) is None
        or not isinstance(ready_at, int)
        or abs(time.time() - ready_at) > 120
    ):
        raise CoordinatorError("watchdog readiness is stale or mismatched")
    try:
        os.kill(pid, 0)
    except OSError as error:
        raise CoordinatorError("release watchdog is not running") from error


def _verification(path: Path, state: dict[str, Any]) -> None:
    try:
        value = json.loads(_read(path, private=True, maximum=64 * 1024))
    except (OSError, ValueError) as error:
        raise CoordinatorError("external verification is unavailable") from error
    expected = ["local-health", "local-identity", "local-auth", "browser-smoke"]
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "transaction", "completedAt", "checks"}
        or value.get("version") != 1
        or value.get("transaction") != state["transaction"]
        or value.get("checks") != expected
        or not isinstance(value.get("completedAt"), int)
        or abs(time.time() - value["completedAt"]) > 120
    ):
        raise CoordinatorError("external verification is stale or mismatched")


def _systemctl(profile: dict[str, Any], *arguments: str) -> None:
    _run(["systemctl", "--user", *arguments], profile, 60)


def _health(profile: dict[str, Any]) -> None:
    request = urllib.request.Request(
        f"http://127.0.0.1:{profile['port']}/healthz",
        headers={"Host": f"127.0.0.1:{profile['port']}"},
    )
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=10) as response:
            value = json.loads(response.read(64 * 1024))
            status = response.status
    except Exception as error:
        raise CoordinatorError("coordinator health check failed") from error
    if status != 200 or not isinstance(value, dict) or value.get("ok") is not True:
        raise CoordinatorError("coordinator health check failed")


def _deploy(
    request: dict[str, Any], profile: dict[str, Any], root: Path, state: dict[str, Any], manifest: Path
) -> dict[str, Any]:
    if state["decision"] == "pending":
        if not _active(profile):
            raise CoordinatorError("pending coordinator is not running")
        _health(profile)
        installed = _live_identity(profile)
        expected = {"version": state["candidate"]["wheelVersion"], "revision": state["candidate"]["revision"]}
        if installed != expected:
            raise CoordinatorError("pending coordinator identity mismatch")
        return _result("pending", installed, state["deadline"])
    if state["decision"] != "prepared":
        raise CoordinatorError(f"deploy requires prepared state, found {state['decision']}")
    _readiness(root / "watchdog-ready.json", state)
    _guard(state, profile)
    if _hash(profile["unit"]) != state["oldUnitSha256"]:
        raise CoordinatorError("coordinator unit changed after prepare")
    proposed_path = root / "guard/coordinator-unit.proposed"
    if _hash(proposed_path, private=True) != state["newUnitSha256"]:
        raise CoordinatorError("proposed coordinator unit changed")
    candidate_identity = {"version": state["candidate"]["wheelVersion"], "revision": state["candidate"]["revision"]}
    if _identity(Path(state["releaseRoot"]), profile) != candidate_identity:
        raise CoordinatorError("staged coordinator identity changed")
    state["cutoverAuthSha256"] = _hash(profile["state_root"] / "auth.json", private=True)
    state["decision"] = "pending"
    state["deadline"] = time.time() + WINDOW_SECONDS
    _save(manifest, state)
    try:
        _atomic(profile["unit"], _read(proposed_path, private=True), state["unitMode"])
        _systemctl(profile, "daemon-reload")
        _systemctl(profile, "restart", profile["service_name"])
        if not _active(profile):
            raise CoordinatorError("candidate coordinator did not start")
        _health(profile)
        if _hash(profile["state_root"] / "auth.json", private=True) != state["cutoverAuthSha256"]:
            raise CoordinatorError("coordinator authentication changed during cutover")
        _guard(state, profile)
    except BaseException as error:
        try:
            _rollback(profile, state, manifest)
        except BaseException as rollback_error:
            raise CoordinatorError("deploy failed and coordinator rollback was incomplete") from rollback_error
        raise CoordinatorError("deploy failed and coordinator was rolled back") from error
    installed = _live_identity(profile)
    if installed != candidate_identity:
        _rollback(profile, state, manifest)
        raise CoordinatorError("running coordinator identity mismatch")
    return _result("pending", installed, state["deadline"])


def _verify(profile: dict[str, Any], root: Path, state: dict[str, Any], manifest: Path) -> dict[str, Any]:
    if state["decision"] == "verified":
        return _result(
            "verified", {"version": state["candidate"]["wheelVersion"], "revision": state["candidate"]["revision"]}
        )
    if state["decision"] != "pending":
        raise CoordinatorError(f"verify requires pending state, found {state['decision']}")
    deadline = state.get("deadline")
    if not isinstance(deadline, (int, float)) or time.time() >= deadline:
        raise CoordinatorError("coordinator verification deadline expired")
    _verification(root / "verification.json", state)
    _guard(state, profile)
    if _hash(profile["unit"]) != state["newUnitSha256"] or not _active(profile):
        raise CoordinatorError("candidate coordinator service identity changed")
    _health(profile)
    installed = _live_identity(profile)
    expected = {"version": state["candidate"]["wheelVersion"], "revision": state["candidate"]["revision"]}
    if installed != expected:
        raise CoordinatorError("running coordinator is not the candidate")
    state["decision"] = "verified"
    state["deadline"] = None
    state["verifiedAt"] = time.time()
    _save(manifest, state)
    return _result("verified", installed)


def _rollback(profile: dict[str, Any], state: dict[str, Any], manifest: Path) -> dict[str, Any]:
    from .coordinator_rollback import rollback

    return rollback(profile, state, manifest)
