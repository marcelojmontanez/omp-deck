import json
import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path


def config_path():
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return Path(
        os.path.abspath(Path(os.environ.get("OMP_DECK_CONFIG", config_home / "omp-deck/config.json")).expanduser())
    )


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    session_root: Path
    socket_root: Path
    state_root: Path
    project_root: Path
    omp_bin: str
    allowed_hosts: tuple[str, ...]

    @property
    def cursor_path(self):
        return self.state_root / "cursors.json"


def expanded(value):
    return Path(os.path.abspath(Path(value).expanduser()))


def allowed_hosts(values, host):
    configured = values.get("allowed_hosts", [])
    if not isinstance(configured, list) or any(
        not isinstance(item, str) or not item or any(character in item for character in "/:@") for item in configured
    ):
        raise RuntimeError("allowed_hosts must be a list of hostnames")
    return tuple(dict.fromkeys([host, "localhost", "127.0.0.1", "::1", *configured]))


def _read_config(path):
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
    state_home = Path(os.environ.get("XDG_STATE_HOME", home / ".local/state"))
    port = int(values.get("port", 8787))
    if not 1 <= port <= 65535:
        raise RuntimeError("port must be between 1 and 65535")
    return Settings(
        host=str(values.get("host", "127.0.0.1")),
        port=port,
        session_root=expanded(values.get("session_root", home / ".omp/agent/sessions")),
        socket_root=expanded(values.get("socket_root", state_home / "omp-deck/sockets")),
        state_root=expanded(values.get("state_root", state_home / "omp-deck")),
        project_root=expanded(values.get("project_root", home / "dev")),
        omp_bin=str(values.get("omp_bin", shutil.which("omp") or home / ".local/bin/omp")),
        allowed_hosts=allowed_hosts(values, str(values.get("host", "127.0.0.1"))),
    )
