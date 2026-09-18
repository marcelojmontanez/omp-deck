from __future__ import annotations

import plistlib
import re
import shlex

from .node_common import Profile, ReleaseError, read_file, run


def environment(profile: Profile) -> dict[str, str]:
    payload = read_file(profile.unit, 1024 * 1024)
    if profile.platform == "darwin":
        try:
            unit = plistlib.loads(payload)
        except (plistlib.InvalidFileException, ValueError) as error:
            raise ReleaseError("invalid LaunchAgent") from error
        expected = [str(profile.tool_python), "-m", "omp_deck", "serve", "--config", str(profile.config)]
        values = unit.get("EnvironmentVariables") if isinstance(unit, dict) else None
        if unit.get("Label") != profile.launch_label or unit.get("ProgramArguments") != expected:
            raise ReleaseError("unexpected LaunchAgent identity")
        if not isinstance(values, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in values.items()
        ):
            raise ReleaseError("invalid LaunchAgent environment")
    else:
        try:
            text = payload.decode()
        except UnicodeDecodeError as error:
            raise ReleaseError("invalid systemd unit encoding") from error
        commands = [line[10:] for line in text.splitlines() if line.startswith("ExecStart=")]
        expected = [str(profile.tool_python), "-m", "omp_deck", "serve"]
        if len(commands) != 1 or shlex.split(commands[0]) != expected:
            raise ReleaseError("unexpected systemd command")
        if not {"Restart=on-failure", "RestartSec=2"}.issubset(set(text.splitlines())):
            raise ReleaseError("systemd restart policy changed")
        manager = run(profile, ["systemctl", "--user", "show-environment"]).stdout.decode().splitlines()
        if f"HOME={profile.home}" not in manager:
            raise ReleaseError("systemd manager HOME changed")
        values = {"HOME": str(profile.home)}
        for line in text.splitlines():
            if line.startswith("Environment="):
                for item in shlex.split(line[12:]):
                    if "=" not in item:
                        raise ReleaseError("invalid systemd environment")
                    key, value = item.split("=", 1)
                    values[key] = value
    if values.get("HOME") != str(profile.home) or values.get("OMP_DECK_CONFIG") != str(profile.config):
        raise ReleaseError("service environment changed")
    return values


def enabled(profile: Profile) -> bool:
    if profile.platform == "linux":
        result = run(profile, ["systemctl", "--user", "is-enabled", profile.service_name], check=False)
        if result.returncode not in {0, 1}:
            raise ReleaseError("cannot inspect systemd enabled state")
        return result.returncode == 0
    output = run(profile, ["/bin/launchctl", "print-disabled", profile.launch_domain]).stdout.decode()
    matches = re.findall(
        rf'(?im)^\s*"{re.escape(profile.launch_label or "")}"\s*=>\s*(enabled|disabled|true|false)\s*;?\s*$', output
    )
    if len(matches) > 1:
        raise ReleaseError("ambiguous LaunchAgent enabled state")
    return not matches or matches[0].casefold() in {"enabled", "false"}
