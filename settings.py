import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path


CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
CONFIG_PATH = Path(os.environ.get("OMP_DECK_CONFIG", CONFIG_HOME / "omp-deck/config.json")).expanduser()


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    session_root: Path
    socket_root: Path
    state_root: Path
    project_root: Path
    omp_bin: str

    @property
    def cursor_path(self):
        return self.state_root / "read-cursors.json"

    @property
    def upload_root(self):
        return self.state_root / "uploads"


def expanded(value):
    return Path(value).expanduser()


def load_settings(path=CONFIG_PATH):
    try:
        values = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        values = {}
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"cannot read {path}: {error}") from error
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
    )


SETTINGS = load_settings()
