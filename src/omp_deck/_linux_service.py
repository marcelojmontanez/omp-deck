import os
import sys
from pathlib import Path


def systemd_quote(value):
    value = str(value)
    if "\0" in value or "\n" in value or "\r" in value:
        raise ValueError("systemd values cannot contain control characters")
    return '"' + value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"') + '"'


def service_unit(config_path, executable=sys.executable):
    environment = {"HOME": str(Path.home()), "OMP_DECK_CONFIG": str(config_path)}
    for name in (
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "OMP_DECK_BIN_DIR",
        "PI_CODING_AGENT_DIR",
    ):
        value = os.environ.get(name)
        if value:
            environment[name] = str(Path(value).expanduser().absolute())
    for name in ("OMP_PROFILE", "PI_PROFILE"):
        value = os.environ.get(name)
        if value:
            environment[name] = value
    service_lines = [
        "[Unit]",
        "Description=OMP Deck",
        "After=default.target",
        "",
        "[Service]",
        "Type=simple",
    ]
    service_lines.extend(f"Environment={systemd_quote(f'{name}={value}')}" for name, value in environment.items())
    service_lines.extend(
        (
            f"ExecStart={systemd_quote(executable)} -m omp_deck serve",
            "Restart=on-failure",
            "RestartSec=2",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        )
    )
    return "\n".join(service_lines)
