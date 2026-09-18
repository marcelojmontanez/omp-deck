from __future__ import annotations

import os
import stat
from pathlib import Path

_APP_NAME = "OMP Deck"
_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400


def _is_link_like(path):
    try:
        info = path.lstat()
    except OSError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT)


def _has_link_like_component(path):
    """Reject user-controlled aliases while allowing system aliases such as macOS /var."""
    absolute = Path(os.path.abspath(Path(path).expanduser()))
    current = Path(absolute.anchor)
    user_controlled = False
    for part in absolute.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            return False
        except OSError:
            return True
        link_like = stat.S_ISLNK(info.st_mode) or bool(
            getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
        )
        if link_like:
            if os.name == "nt" or user_controlled or info.st_uid != 0:
                return True
            continue
        if os.name != "nt" and info.st_uid == os.geteuid():
            user_controlled = True
    return False


def config_home() -> Path:
    if os.name == "nt":
        value = os.environ.get("APPDATA")
        return Path(value) if value else Path.home() / "AppData/Roaming"
    value = os.environ.get("XDG_CONFIG_HOME")
    return Path(value) if value else Path.home() / ".config"


def data_home() -> Path:
    if os.name == "nt":
        value = os.environ.get("LOCALAPPDATA")
        return Path(value) if value else Path.home() / "AppData/Local"
    value = os.environ.get("XDG_DATA_HOME")
    return Path(value) if value else Path.home() / ".local/share"


def state_home() -> Path:
    if os.name == "nt":
        return data_home()
    value = os.environ.get("XDG_STATE_HOME")
    return Path(value) if value else Path.home() / ".local/state"


def default_config_path() -> Path:
    name = _APP_NAME if os.name == "nt" else "omp-deck"
    return config_home() / name / "config.json"


def default_state_root() -> Path:
    if os.name == "nt":
        return state_home() / _APP_NAME / "state"
    return state_home() / "omp-deck"


def default_install_root() -> Path:
    name = _APP_NAME if os.name == "nt" else "omp-deck"
    root = data_home() / name
    return root / "install" if os.name == "nt" else root


def default_plugin_root() -> Path:
    if os.name == "nt":
        return data_home() / _APP_NAME / "plugin"
    return data_home() / "omp-deck-plugin"


def default_omp_path() -> Path:
    if os.name == "nt":
        return data_home() / "omp" / "omp.exe"
    return Path.home() / ".local/bin/omp"
