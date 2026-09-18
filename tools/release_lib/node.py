from __future__ import annotations

import contextlib
import json
import os
import shutil
import time
import urllib.parse
from pathlib import Path
from typing import Any

from .node_artifacts import inspect_wheel
from .node_common import (
    HEX40,
    HEX64,
    TX,
    ReleaseError,
    atomic_json,
    digest,
    health,
    lock,
    pid,
    profile,
    quiescent,
    read_file,
    registry,
    require_dir,
    require_runtime,
    restart_token,
    run,
    running,
    session_snapshot,
    start,
    stop,
    tree_digest,
)
from .node_service import enabled as service_enabled
from .node_service import environment as service_environment

WINDOW = 300


def _context(request):
    if not isinstance(request, dict) or request.get("version") != 1 or request.get("kind") != "node":
        raise ReleaseError("unsupported node request")
    tx = request.get("transaction")
    if not isinstance(tx, str) or TX.fullmatch(tx) is None:
        raise ReleaseError("invalid transaction")
    root = Path(request.get("root", ""))
    if not root.is_absolute():
        raise ReleaseError("release root must be absolute")
    require_dir(root, private=True)
    p = profile(request.get("profile"))
    require_runtime(p)
    origin = request.get("origin")
    if origin is not None:
        if not isinstance(origin, str):
            raise ReleaseError("invalid origin")
        parsed = urllib.parse.urlsplit(origin)
        public_https = parsed.scheme == "https" and bool(parsed.hostname)
        loopback_http = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "::1"}
        if not (public_https or loopback_http) or parsed.username or parsed.password:
            raise ReleaseError("origin must be public HTTPS or loopback HTTP")
    if root != p.rollback_root / tx:
        raise ReleaseError("release root does not match profile rollback root and transaction")
    return p, root, tx


def _artifact(value, label):
    if not isinstance(value, dict):
        raise ReleaseError(f"invalid {label} artifact")
    required = {"wheelPath", "wheelSha256", "wheelVersion", "revision", "pluginSha256"}
    if (
        not required <= set(value)
        or HEX64.fullmatch(str(value["wheelSha256"])) is None
        or HEX64.fullmatch(str(value["pluginSha256"])) is None
        or HEX40.fullmatch(str(value["revision"])) is None
    ):
        raise ReleaseError(f"invalid {label} identity")
    path = Path(value["wheelPath"])
    if not path.is_absolute():
        raise ReleaseError(f"{label} wheel path must be absolute")
    sha, version, plugin = inspect_wheel(path)
    if (sha, version, plugin) != (value["wheelSha256"], value["wheelVersion"], value["pluginSha256"]):
        raise ReleaseError(f"{label} wheel identity mismatch")
    return {k: value[k] for k in required}


def _manifest(root):
    try:
        value = json.loads(read_file(root / "guard/manifest.json", 2 * 1024 * 1024, private=True))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ReleaseError("invalid release manifest") from e
    if not isinstance(value, dict) or value.get("version") != 1:
        raise ReleaseError("unsupported release manifest")
    return value


def _save(root, m):
    m["updatedAt"] = int(time.time())
    atomic_json(root / "guard/manifest.json", m)


def _installed(p):
    code = "import json,omp_deck;print(json.dumps(omp_deck.build_identity()))"
    value = json.loads(run(p, [p.tool_python, "-c", code], timeout=30).stdout)
    if not isinstance(value, dict) or set(value) != {"version", "revision"}:
        raise ReleaseError("installed build identity is invalid")
    return value


def _plugin_path(p):
    data = json.loads(run(p, [p.omp, "plugin", "list", "--json"], timeout=30).stdout)
    matches = [item for item in data.get("npm", []) if isinstance(item, dict) and item.get("name") == "omp-deck"]
    if len(matches) != 1:
        raise ReleaseError("expected exactly one omp-deck plugin")
    actual = Path(matches[0].get("path", "")).resolve(strict=True)
    if actual != p.plugin_expected.resolve(strict=True):
        raise ReleaseError("plugin path differs from authorized path")
    return actual


def _guard_files(p, m):
    if digest(p.config) != m["originals"]["configSha256"] or digest(p.unit) != m["originals"]["unitSha256"]:
        raise ReleaseError("service configuration changed")


@contextlib.contextmanager
def _admit(p, cookie):
    first_pid = pid(p)
    first_reg, first_stable = registry(p)
    first = restart_token(session_snapshot(p, cookie), first_reg)
    with lock(p.ledger.with_name(".requests.json.lock")):
        second_pid = pid(p)
        second_reg, second_stable = registry(p)
        second = restart_token(session_snapshot(p, cookie), second_reg)
        if (first_pid, first_stable, first) != (second_pid, second_stable, second):
            raise ReleaseError("restart admission changed between snapshots")
        yield {"pid": second_pid, "registryStable": second_stable, "authSha256": digest(p.auth)}


def _verify_staged(m):
    for label in ("candidate", "rollback"):
        artifact = m[label]
        sha, version, plugin = inspect_wheel(Path(artifact["wheelPath"]))
        if (sha, version, plugin) != (artifact["wheelSha256"], artifact["wheelVersion"], artifact["pluginSha256"]):
            raise ReleaseError(f"{label} staged artifact changed")
    if digest(Path(m["constraints"])) != m["constraintsSha256"]:
        raise ReleaseError("frozen constraints changed")


def _uv_environment(p):
    return {
        "UV_TOOL_DIR": str(p.tool_root.parent),
        "UV_TOOL_BIN_DIR": str(p.entrypoint.parent),
        "UV_PYTHON_DOWNLOADS": "never",
    }


def _install(p, a, constraints):
    run(
        p,
        [
            p.uv,
            "tool",
            "install",
            "--offline",
            "--reinstall",
            "--python",
            p.base_python,
            "--constraints",
            constraints,
            a["wheelPath"],
        ],
        timeout=300,
        env=_uv_environment(p),
    )
    if _installed(p) != {"version": a["wheelVersion"], "revision": a["revision"]}:
        raise ReleaseError("installed backend identity mismatch")
    if not p.entrypoint.is_symlink() or p.entrypoint.resolve(strict=True) != p.tool_root / "bin/omp-deck":
        raise ReleaseError("uv launcher identity changed")


def _install_plugin(p, expected, env, backup=None):
    if tree_digest(_plugin_path(p)) == expected:
        return False
    cmd = [p.omp, "plugin", "install", backup] if backup else [p.tool_python, "-m", "omp_deck", "plugin", "install"]
    run(p, cmd, env=env, timeout=180)
    if tree_digest(_plugin_path(p)) != expected:
        raise ReleaseError("plugin identity mismatch after installation")
    return True


def prepare(request):
    p, root, tx = _context(request)
    guard = root / "guard"
    guard.mkdir(mode=0o700, exist_ok=True)
    require_dir(guard, private=True)
    with lock(guard / "transaction.lock"):
        if (guard / "manifest.json").exists():
            return status(request)
        environment = service_environment(p)
        enabled = service_enabled(p)
        candidate = _artifact(request.get("candidate"), "candidate")
        rollback = _artifact(request.get("rollback"), "rollback")
        if candidate["wheelSha256"] == rollback["wheelSha256"]:
            raise ReleaseError("candidate and rollback artifacts are identical")
        for executable in (p.uv, p.base_python, p.omp, p.tool_python):
            if not executable.is_file() or not os.access(executable, os.X_OK):
                raise ReleaseError("required executable unavailable")
        if (
            not running(p)
            or _installed(p) != {"version": rollback["wheelVersion"], "revision": rollback["revision"]}
            or tree_digest(_plugin_path(p)) != rollback["pluginSha256"]
        ):
            raise ReleaseError("installed rollback identity mismatch")
        read_file(p.auth, 1024 * 1024, private=True)
        reg, stable = registry(p)
        snapshots = guard / "snapshots"
        snapshots.mkdir(mode=0o700)
        shutil.copytree(p.tool_root, snapshots / "uv-tool-environment", symlinks=True)
        shutil.copytree(
            _plugin_path(p), snapshots / "plugin", symlinks=False, ignore=shutil.ignore_patterns("node_modules")
        )
        frozen = run(p, [p.uv, "pip", "freeze", "--python", p.tool_python], timeout=60).stdout
        constraints = (
            b"\n".join(x for x in frozen.splitlines() if not x.startswith((b"omp-deck==", b"omp-deck @ "))) + b"\n"
        )
        cpath = guard / "runtime-constraints.txt"
        cpath.write_bytes(constraints)
        cpath.chmod(0o600)
        run(
            p,
            [
                p.uv,
                "pip",
                "install",
                "--dry-run",
                "--python",
                p.tool_python,
                "--constraints",
                cpath,
                candidate["wheelPath"],
            ],
            timeout=300,
            env=_uv_environment(p),
        )
        m = {
            "version": 1,
            "transaction": tx,
            "state": "prepared",
            "deadline": None,
            "candidate": candidate,
            "rollback": rollback,
            "constraints": str(cpath),
            "constraintsSha256": digest(cpath),
            "pluginActivation": "unchanged",
            "touched": False,
            "loopbackHTTP": request.get("loopbackHTTP"),
            "originals": {
                "configSha256": digest(p.config),
                "unitSha256": digest(p.unit),
                "authSha256": digest(p.auth),
                "registryStable": stable,
                "registryGeneration": reg.get("generation"),
                "pluginBackup": str(snapshots / "plugin"),
                "environmentBackup": str(snapshots / "uv-tool-environment"),
                "serviceEnvironment": environment,
                "serviceEnabled": enabled,
            },
            "lastError": None,
        }
        _save(root, m)
    return status(request)


def _ready(root, tx):
    try:
        x = json.loads(read_file(root / "watchdog-ready.json", 64 * 1024, private=True))
    except Exception as e:
        raise ReleaseError("watchdog readiness marker unavailable") from e
    now = int(time.time())
    process = x.get("pid") if isinstance(x, dict) else None
    if (
        set(x) != {"version", "transaction", "pid", "nonce", "readyAt"}
        or x.get("version") != 1
        or x.get("transaction") != tx
        or not isinstance(process, int)
        or isinstance(process, bool)
        or process <= 0
        or not isinstance(x.get("nonce"), str)
        or HEX64.fullmatch(x["nonce"]) is None
        or not isinstance(x.get("readyAt"), int)
        or isinstance(x.get("readyAt"), bool)
        or not 0 <= now - x["readyAt"] <= 120
    ):
        raise ReleaseError("invalid or stale watchdog readiness marker")
    try:
        os.kill(process, 0)
    except OSError as e:
        raise ReleaseError("watchdog process is not alive") from e


def deploy(request):
    p, root, tx = _context(request)
    _ready(root, tx)
    with lock(root / "guard/transaction.lock"):
        m = _manifest(root)
        if m["state"] == "pending":
            return status(request)
        if m["state"] != "prepared":
            raise ReleaseError("deploy requires prepared state")
        _verify_staged(m)
        _guard_files(p, m)
        m["state"] = "pending"
        m["deadline"] = time.time() + WINDOW
        _save(root, m)
        try:
            cookie = request.get("cookie_file")
            with _admit(p, cookie) as cutover:
                m["cutover"] = cutover
                m["touched"] = True
                _save(root, m)
                stop(p, cutover["pid"])
            _install(p, m["candidate"], m["constraints"])
            _install_plugin(p, m["candidate"]["pluginSha256"], m["originals"]["serviceEnvironment"])
            if m["candidate"]["pluginSha256"] != m["rollback"]["pluginSha256"]:
                m["pluginActivation"] = "restart-required"
            _guard_files(p, m)
            start(p)
            health(p, base_url=m.get("loopbackHTTP"))
            if service_enabled(p) != m["originals"]["serviceEnabled"]:
                raise ReleaseError("service enabled policy changed")
            _save(root, m)
        except BaseException as e:
            m["lastError"] = f"deploy failed: {type(e).__name__}"
            _save(root, m)
            try:
                _rollback_locked(p, root, m, request.get("cookie_file"), "automatic rollback after deploy failure")
            except BaseException as r:
                m["state"] = "rollback_failed"
                m["lastError"] = f"deploy failed; rollback failed: {type(r).__name__}"
                _save(root, m)
                raise ReleaseError(m["lastError"]) from e
            raise ReleaseError("deployment failed and was rolled back") from e
    return status(request)


def _rollback_locked(p, root, m, cookie, reason):
    if m["state"] == "rolledback":
        return
    if not m.get("touched"):
        m.update(state="rolledback", deadline=None, lastError=reason)
        _save(root, m)
        return
    _guard_files(p, m)
    _verify_staged(m)
    auth_before = digest(p.auth)
    if running(p):
        with _admit(p, cookie) as current:
            if current["registryStable"] != m["cutover"]["registryStable"]:
                raise ReleaseError("registry identities changed; refusing rollback")
            stop(p, current["pid"])
    elif not quiescent(p):
        raise ReleaseError("service ownership uncertain; refusing rollback")
    backup = Path(m["originals"]["environmentBackup"])
    if not backup.is_dir() or backup.is_symlink():
        raise ReleaseError("retained rollback environment unavailable")
    failed = p.tool_root.with_name(f".{p.tool_root.name}.failed-{m['transaction']}")
    if failed.exists() or failed.is_symlink():
        raise ReleaseError("rollback swap destination already exists")
    moved = p.tool_root.exists() and not p.tool_root.is_symlink()
    if moved:
        os.replace(p.tool_root, failed)
    try:
        shutil.copytree(backup, p.tool_root, symlinks=True)
        expected = {"version": m["rollback"]["wheelVersion"], "revision": m["rollback"]["revision"]}
        if _installed(p) != expected:
            raise ReleaseError("retained rollback environment identity mismatch")
    except BaseException:
        shutil.rmtree(p.tool_root, ignore_errors=True)
        if moved:
            os.replace(failed, p.tool_root)
        raise
    if moved:
        shutil.rmtree(failed)
    _install_plugin(
        p, m["rollback"]["pluginSha256"], m["originals"]["serviceEnvironment"], m["originals"]["pluginBackup"]
    )
    start(p)
    health(p, base_url=m.get("loopbackHTTP"))
    if service_enabled(p) != m["originals"]["serviceEnabled"]:
        raise ReleaseError("service enabled policy changed during rollback")
    if digest(p.auth) != auth_before or registry(p)[1] != m["cutover"]["registryStable"]:
        raise ReleaseError("durable identity changed during rollback")
    m.update(state="rolledback", deadline=None, lastError=reason, pluginActivation="unchanged")
    _save(root, m)


def rollback(request):
    p, root, _ = _context(request)
    with lock(root / "guard/transaction.lock"):
        m = _manifest(root)
        try:
            _rollback_locked(p, root, m, request.get("cookie_file"), "explicit guarded rollback")
        except BaseException as e:
            m["state"] = "rollback_failed"
            m["lastError"] = f"rollback failed: {type(e).__name__}"
            _save(root, m)
            raise
    return status(request)


def verify(request):
    p, root, tx = _context(request)
    with lock(root / "guard/transaction.lock"):
        m = _manifest(root)
        if m["state"] == "verified":
            return status(request)
        if m["state"] != "pending" or time.time() >= m["deadline"]:
            raise ReleaseError("verification requires unexpired pending state")
        marker = json.loads(read_file(root / "verification.json", 64 * 1024, private=True))
        checks = ["local-health", "local-identity", "local-auth", "browser-smoke"]
        supplied = request.get("worker_verification")
        now = int(time.time())
        if (
            marker != supplied
            or set(marker) != {"version", "transaction", "completedAt", "checks"}
            or marker.get("version") != 1
            or marker.get("transaction") != tx
            or marker.get("checks") != checks
            or not isinstance(marker.get("completedAt"), int)
            or isinstance(marker.get("completedAt"), bool)
            or not 0 <= now - marker["completedAt"] <= 120
        ):
            raise ReleaseError("worker verification is incomplete")
        health(p, base_url=m.get("loopbackHTTP"))
        _guard_files(p, m)
        if (
            _installed(p) != {"version": m["candidate"]["wheelVersion"], "revision": m["candidate"]["revision"]}
            or tree_digest(_plugin_path(p)) != m["candidate"]["pluginSha256"]
        ):
            raise ReleaseError("candidate identity changed")
        m.update(state="verified", deadline=None, lastError=None)
        _save(root, m)
    return status(request)


def status(request):
    p, root, _ = _context(request)
    path = root / "guard/manifest.json"
    if not path.exists():
        return {"version": 1, "state": "absent", "pluginActivation": "unchanged"}
    m = _manifest(root)
    result = {"version": 1, "state": m["state"], "pluginActivation": m.get("pluginActivation", "unchanged")}
    try:
        result["installed"] = _installed(p)
    except ReleaseError:
        pass
    if m.get("deadline") is not None:
        result["deadline"] = m["deadline"]
    return result


def execute(action: str, request: dict[str, Any]) -> dict[str, Any]:
    functions = {"prepare": prepare, "status": status, "deploy": deploy, "verify": verify, "rollback": rollback}
    if action not in functions:
        raise ReleaseError("unsupported node release action")
    return functions[action](request)
