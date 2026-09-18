import os
import shutil
import sys
from pathlib import Path


def systemd_quote(value):
    value = str(value)
    if "\0" in value or "\n" in value or "\r" in value:
        raise ValueError("systemd values cannot contain control characters")
    return '"' + value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"') + '"'


def service_unit(config_path, omp_bin=None, executable=sys.executable):
    environment = {"HOME": str(Path.home()), "OMP_DECK_CONFIG": str(config_path)}
    if omp_bin is not None:
        resolved_omp = shutil.which(str(omp_bin))
        if resolved_omp is None:
            candidate = Path(omp_bin).expanduser()
            if not candidate.is_absolute():
                raise RuntimeError("cannot resolve the configured OMP executable")
            resolved_omp = str(candidate)
        environment["OMP_DECK_OMP_BIN"] = str(Path(resolved_omp).absolute())
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
