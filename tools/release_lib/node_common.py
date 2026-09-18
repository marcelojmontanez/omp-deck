from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import stat
import subprocess
import time
import urllib.parse
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from email.header import Header
from pathlib import Path
from typing import Any

from omp_deck._private_state import PrivateStateFile

if os.name != "nt":
    import fcntl
    import pwd

TX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
HEX40 = re.compile(r"^[a-f0-9]{40}$")
HEX64 = re.compile(r"^[a-f0-9]{64}$")


class ReleaseError(RuntimeError):
    pass


@dataclass(frozen=True)
class Profile:
    platform: str
    user: str
    home: Path
    uv: Path
    base_python: Path
    omp: Path
    tool_root: Path
    entrypoint: Path
    config: Path
    unit: Path
    state_root: Path
    plugin_expected: Path
    service_name: str
    rollback_root: Path
    port: int
    launch_domain: str | None = None
    launch_label: str | None = None

    @property
    def tool_python(self):
        return self.tool_root / "bin/python"

    @property
    def registry(self):
        return self.state_root / "registry.json"

    @property
    def ledger(self):
        return self.state_root / "requests.json"

    @property
    def auth(self):
        return self.state_root / "auth.json"

    @property
    def launch_target(self):
        if not self.launch_domain or not self.launch_label:
            raise ReleaseError("missing launchd identity")
        return f"{self.launch_domain}/{self.launch_label}"


def profile(value: Any) -> Profile:
    names = {
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
    }
    if not isinstance(value, dict) or set(value) - names or not names - {"launch_domain", "launch_label"} <= set(value):
        raise ReleaseError("invalid node profile")
    try:
        kw = {
            k: (
                Path(v)
                if k
                in {
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
                    "rollback_root",
                }
                else v
            )
            for k, v in value.items()
        }
        p = Profile(**kw)
    except (TypeError, ValueError) as e:
        raise ReleaseError("invalid node profile") from e
    if (
        p.platform not in {"darwin", "linux"}
        or not isinstance(p.port, int)
        or isinstance(p.port, bool)
        or not 1 <= p.port <= 65535
    ):
        raise ReleaseError("invalid node platform or port")
    paths = [
        p.home,
        p.uv,
        p.base_python,
        p.omp,
        p.tool_root,
        p.entrypoint,
        p.config,
        p.unit,
        p.state_root,
        p.plugin_expected,
        p.rollback_root,
    ]
    if any(not x.is_absolute() or ".." in x.parts for x in paths):
        raise ReleaseError("profile paths must be absolute and normalized")
    if p.platform == "darwin" and (not p.launch_domain or not p.launch_label):
        raise ReleaseError("macOS profile requires launch identity")
    return p


def require_runtime(p: Profile):
    if p.platform != __import__("sys").platform:
        raise ReleaseError("profile platform does not match runtime")
    if pwd.getpwuid(os.geteuid()).pw_name != p.user or Path.home() != p.home:
        raise ReleaseError("profile user/home does not match runtime")


def require_dir(path: Path, private=False, create=False):
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=0o700 if private else 0o755)
    try:
        s = path.stat(follow_symlinks=False)
    except OSError as e:
        raise ReleaseError("required directory unavailable") from e
    if (
        not stat.S_ISDIR(s.st_mode)
        or path.is_symlink()
        or s.st_uid != os.geteuid()
        or (private and stat.S_IMODE(s.st_mode) != 0o700)
    ):
        raise ReleaseError("unsafe directory")


def read_file(path: Path, maximum=128 * 1024 * 1024, private=False):
    try:
        s = path.stat(follow_symlinks=False)
    except OSError as e:
        raise ReleaseError("required file unavailable") from e
    if (
        path.is_symlink()
        or not stat.S_ISREG(s.st_mode)
        or s.st_uid != os.geteuid()
        or s.st_size > maximum
        or (private and s.st_mode & 0o077)
    ):
        raise ReleaseError("unsafe file")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        now = os.fstat(fd)
        if (now.st_dev, now.st_ino, now.st_size) != (s.st_dev, s.st_ino, s.st_size):
            raise ReleaseError("file changed while opening")
        data = os.read(fd, maximum + 1)
        if len(data) > maximum:
            raise ReleaseError("file too large")
        return data
    finally:
        os.close(fd)


def atomic_write(path: Path, payload: bytes, mode: int = 0o600):
    if mode != 0o600:
        raise ReleaseError("private release files must be mode 0600")
    storage = PrivateStateFile(path, repair_root=False)
    with storage.locked():
        storage.replace(payload)


def atomic_json(path: Path, value: dict):
    payload = (json.dumps(value, sort_keys=True) + "\n").encode()
    atomic_write(path, payload)


def digest(path):
    return hashlib.sha256(read_file(Path(path))).hexdigest()


def tree_digest(root: Path):
    require_dir(root)
    result = hashlib.sha256()
    for item in sorted(root.rglob("*")):
        relative = item.relative_to(root)
        if relative.parts and relative.parts[0] == "node_modules":
            continue
        if item.is_symlink():
            raise ReleaseError("plugin tree contains symlink")
        if item.is_dir():
            continue
        info = item.stat(follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode):
            raise ReleaseError("unsafe plugin entry")
        result.update(relative.as_posix().encode())
        result.update(b"\0")
        result.update(item.read_bytes())
        result.update(b"\0")
    return result.hexdigest()


def run(p: Profile, args, timeout=180, check=True, env=None):
    try:
        r = subprocess.run(
            [str(x) for x in args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            env={**os.environ, "HOME": str(p.home), **(env or {})},
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ReleaseError("release command could not complete") from e
    if check and r.returncode:
        raise ReleaseError(f"release command failed ({r.returncode})")
    return r


@contextlib.contextmanager
def lock(path: Path) -> Iterator[None]:
    if os.name == "nt":
        raise ReleaseError("native release guards require macOS or Linux")
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise ReleaseError("unsafe release lock")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def running(p):
    cmd = (
        ["/bin/launchctl", "print", p.launch_target]
        if p.platform == "darwin"
        else ["systemctl", "--user", "is-active", "--quiet", p.service_name]
    )
    return run(p, cmd, check=False).returncode == 0


def pid(p):
    if not running(p):
        raise ReleaseError("service is not running")
    cmd = (
        ["/bin/launchctl", "print", p.launch_target]
        if p.platform == "darwin"
        else ["systemctl", "--user", "show", "--property=MainPID", "--value", p.service_name]
    )
    out = run(p, cmd).stdout.decode(errors="strict")
    if p.platform == "darwin":
        found = re.findall(r"\bpid = ([0-9]+)", out)
        if len(found) != 1:
            raise ReleaseError("ambiguous service pid")
        out = found[0]
    if not out.strip().isdigit() or int(out) <= 1:
        raise ReleaseError("invalid service pid")
    return int(out.strip())


def stop(p, expected):
    if pid(p) != expected:
        raise ReleaseError("service pid changed before stop")
    if p.platform == "darwin":
        run(p, ["/bin/launchctl", "bootout", p.launch_target], timeout=30)
    else:
        run(p, ["systemctl", "--user", "stop", p.service_name])
    deadline = time.monotonic() + 15
    while not quiescent(p):
        if time.monotonic() >= deadline:
            raise ReleaseError("service did not stop")
        time.sleep(0.05)


def quiescent(p):
    if p.platform == "darwin":
        return not running(p)
    state = run(p, ["systemctl", "--user", "show", "--property=ActiveState", "--value", p.service_name], check=False)
    process = run(p, ["systemctl", "--user", "show", "--property=MainPID", "--value", p.service_name], check=False)
    return state.returncode == 0 and state.stdout.strip() in {b"inactive", b"failed"} and process.stdout.strip() == b"0"


def start(p):
    if not quiescent(p):
        raise ReleaseError("service ownership uncertain")
    if p.platform == "darwin":
        run(p, ["/bin/launchctl", "bootstrap", p.launch_domain, p.unit], timeout=120)
    else:
        run(p, ["systemctl", "--user", "start", p.service_name])


def health(p, timeout=20.0, base_url=None):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    base = base_url or f"http://127.0.0.1:{p.port}"
    parsed = urllib.parse.urlsplit(base)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ReleaseError("health endpoint must be loopback HTTP")
    request = urllib.request.Request(
        f"{base.rstrip('/')}/healthz",
        headers={"Host": f"127.0.0.1:{p.port}"},
    )
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            with opener.open(request, timeout=min(1.0, max(0.1, deadline - time.monotonic()))) as response:
                payload = json.loads(response.read(1024 * 1024 + 1))
            if (
                response.status == 200
                and isinstance(payload, dict)
                and all(payload.get(key) is True for key in ("ok", "configuration", "authentication"))
            ):
                return
            last = ReleaseError("unhealthy response")
        except Exception as error:
            last = error
        time.sleep(0.1)
    raise ReleaseError("bounded localhost health check failed") from last


def registry(p):
    from omp_deck.registry_store import RegistryStore

    store = RegistryStore(p.registry)
    try:
        data = (
            json.loads(read_file(p.registry, 8 * 1024 * 1024, private=True)) if p.registry.exists() else store._empty()
        )
        # Inspect without the ordinary recovery path quarantining or upgrading
        # production state. The domain store remains the schema authority.
        store._validate_store(data)
    except (ValueError, UnicodeError) as error:
        raise ReleaseError("invalid registry") from error
    stable = [
        {key: value for key, value in record.items() if key not in {"generation", "updatedAt"}}
        for record in data["sessions"]
    ]
    return data, sorted(stable, key=lambda item: item["id"])


def session_snapshot(p, cookie_file):
    try:
        cookie = json.loads(read_file(Path(cookie_file), 4096, private=True))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReleaseError("invalid cookie file") from error
    if not isinstance(cookie, dict) or set(cookie) != {"version", "credential", "deviceId"} or cookie["version"] != 1:
        raise ReleaseError("invalid cookie file")
    credential = cookie["credential"]
    if not isinstance(credential, str) or re.fullmatch(r"v1\.[A-Za-z0-9_-]{20,256}", credential) is None:
        raise ReleaseError("invalid device credential")
    headers = {"Host": f"127.0.0.1:{p.port}", "Cookie": f"omp_deck_device={credential}"}
    config = json.loads(read_file(p.config, 64 * 1024, private=True))
    mode = config.get("auth_mode", "pairing")
    if mode == "tailscale":
        owner = config.get("tailscale_user")
        if not isinstance(owner, str) or not owner:
            raise ReleaseError("Tailscale owner identity unavailable")
        headers["Tailscale-User-Login"] = owner if owner.isascii() else Header(owner, "utf-8").encode()
    elif mode != "pairing":
        raise ReleaseError("unsupported authentication mode")
    request = urllib.request.Request(f"http://127.0.0.1:{p.port}/api/sessions", headers=headers)
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=10) as response:
            value = json.loads(response.read(8 * 1024 * 1024 + 1))
    except Exception as error:
        raise ReleaseError("authenticated session snapshot unavailable") from error
    if response.status != 200 or not isinstance(value, dict) or not isinstance(value.get("sessions"), list):
        raise ReleaseError("invalid session snapshot")
    return value


def restart_token(snapshot, reg):
    public = {}
    for item in snapshot["sessions"]:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or item["id"] in public:
            raise ReleaseError("ambiguous session ownership")
        public[item["id"]] = item
    rpc_public = {
        key
        for key, value in public.items()
        if isinstance(value.get("transport"), dict) and value["transport"].get("kind") == "rpc"
    }
    rpc_registry = {item["id"] for item in reg["sessions"] if item.get("adapterKind") == "rpc"}
    if rpc_public != rpc_registry:
        raise ReleaseError("live and durable RPC ownership differ")
    token = []
    for item in reg["sessions"]:
        if item.get("adapterKind") != "rpc":
            continue
        session = public[item["id"]]
        transport = session.get("transport")
        if not isinstance(transport, dict) or session.get("state") in {"working", "needs_input"}:
            raise ReleaseError("running RPC is not restart-safe")
        if item.get("desiredRpcRunning"):
            if (
                session.get("lifecycle") != "ongoing"
                or session.get("state") not in {"idle", "failed"}
                or transport.get("status") != "connected"
                or not transport.get("instanceId")
            ):
                raise ReleaseError("running RPC ownership uncertain")
        elif session.get("lifecycle") != "history" or transport.get("instanceId") is not None:
            raise ReleaseError("stopped RPC ownership uncertain")
        token.append(
            (
                item["id"],
                item.get("desiredRpcRunning"),
                session.get("lifecycle"),
                session.get("state"),
                transport.get("status"),
                transport.get("instanceId"),
            )
        )
    return tuple(sorted(token))
