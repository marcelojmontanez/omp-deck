import json
import os
import shutil
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from ._platform_paths import default_config_path, default_omp_path, default_state_root
from ._private_state import validate_private_directory, validate_private_file
from .auth_policy import auth_configuration


def config_path():
    default = default_config_path()
    return Path(os.path.abspath(Path(os.environ.get("OMP_DECK_CONFIG", default)).expanduser()))


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    session_root: Path
    socket_root: Path
    state_root: Path
    project_root: Path
    project_discovery_depth: int
    omp_bin: str
    allowed_hosts: tuple[str, ...]
    auth_mode: str = "pairing"
    tailscale_user: str | None = None
    show_history: bool = False
    save_drafts: bool = True

    @property
    def cursor_path(self):
        return self.state_root / "cursors.json"


def expanded(value):
    return Path(os.path.abspath(Path(value).expanduser()))


def executable(value):
    value = str(value)
    expanded_value = str(Path(value).expanduser())
    if expanded_value == value:
        return value
    return os.path.abspath(expanded_value)


def allowed_hosts(values, host):
    configured = values.get("allowed_hosts", [])
    if not isinstance(configured, list) or any(
        not isinstance(item, str) or not item or any(character in item for character in "/:@") for item in configured
    ):
        raise RuntimeError("allowed_hosts must be a list of hostnames")
    return tuple(dict.fromkeys([host, "localhost", "127.0.0.1", "::1", *configured]))


def _read_config(path):
    if os.name == "nt":
        if not path.exists() and not path.is_symlink():
            return {}
        try:
            validate_private_directory(path.parent)
            validate_private_file(path, 64 * 1024)
            raw = path.read_bytes()
        except OSError as error:
            raise RuntimeError(f"cannot read {path}: {error}") from error
        try:
            return json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError(f"cannot read {path}: {error}") from error
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return {}
    except OSError as error:
        raise RuntimeError(f"cannot read {path}: {error}") from error
    try:
        info = os.fstat(descriptor)
        try:
            parent = path.parent.stat(follow_symlinks=False)
        except OSError as error:
            raise RuntimeError(f"cannot read {path}: {error}") from error
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
            or stat.S_ISLNK(parent.st_mode)
            or not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != os.geteuid()
            or parent.st_mode & 0o022
        ):
            raise RuntimeError(f"{path} has unsafe ownership or permissions")
        raw = os.read(descriptor, 64 * 1024 + 1)
        if len(raw) > 64 * 1024:
            raise RuntimeError(f"{path} is too large")
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"cannot read {path}: {error}") from error
    finally:
        os.close(descriptor)


def load_settings(path=None):
    path = config_path() if path is None else Path(path).expanduser()
    values = _read_config(path)
    if not isinstance(values, dict):
        raise RuntimeError(f"{path} must contain a JSON object")

    home = Path.home()
    state_root = default_state_root()
    port = int(values.get("port", 8787))
    if not 1 <= port <= 65535:
        raise RuntimeError("port must be between 1 and 65535")
    project_discovery_depth = values.get("project_discovery_depth", 2)
    if type(project_discovery_depth) is not int or not 1 <= project_discovery_depth <= 8:
        raise RuntimeError("project_discovery_depth must be an integer between 1 and 8")
    host = str(values.get("host", "127.0.0.1"))
    auth_mode, tailscale_user = auth_configuration(values, host)
    show_history = values.get("show_history", False)
    if type(show_history) is not bool:
        raise RuntimeError("show_history must be a boolean")
    save_drafts = values.get("save_drafts", True)
    if type(save_drafts) is not bool:
        raise RuntimeError("save_drafts must be a boolean")
    configured_omp = values.get("omp_bin", shutil.which("omp") or default_omp_path())
    if sys.platform == "linux":
        configured_omp = os.environ.get("OMP_DECK_OMP_BIN", configured_omp)
    return Settings(
        host=host,
        port=port,
        session_root=expanded(values.get("session_root", home / ".omp/agent/sessions")),
        socket_root=expanded(values.get("socket_root", state_root / "sockets")),
        state_root=expanded(values.get("state_root", state_root)),
        project_root=expanded(values.get("project_root", home / "dev")),
        project_discovery_depth=project_discovery_depth,
        omp_bin=executable(configured_omp),
        allowed_hosts=allowed_hosts(values, host),
        auth_mode=auth_mode,
        tailscale_user=tailscale_user,
        show_history=show_history,
        save_drafts=save_drafts,
    )
