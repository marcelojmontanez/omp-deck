from __future__ import annotations

import os
from pathlib import Path

_APP_NAME = "OMP Deck"


def config_home() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))


def data_home() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))


def state_home() -> Path:
    if os.name == "nt":
        return data_home()
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))


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


def default_omp_path() -> Path:
    if os.name == "nt":
        return data_home() / "omp" / "omp.exe"
    return Path.home() / ".local/bin/omp"
