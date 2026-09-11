from __future__ import annotations

import os
import plistlib
import re
import shutil
import stat
import sys
import time
from pathlib import Path

from .installation_paths import (
    has_ownership_marker,
    installation_paths,
    validate_manifest,
    validate_ownership_claim,
)
from .lifecycle import (
    ServiceSnapshot,
    _capture_roots,
    _file_snapshot,
    _restore_root,
    _restore_service_files,
    prepare_installation,
    write_atomic,
)

_LABEL = "com.omp-deck.agent"
_INHERITED_PATH_ENVIRONMENT = (
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
    "OMP_DECK_BIN_DIR",
    "PI_CODING_AGENT_DIR",
)
_INHERITED_SCALAR_ENVIRONMENT = ("OMP_PROFILE", "PI_PROFILE")


def _result(runner, command, *, check=True, capture_output=False):
    return runner(list(command), check=check, capture_output=capture_output)


def _domain() -> str:
    return f"gui/{os.geteuid()}"


def _target() -> str:
    return f"{_domain()}/{_LABEL}"


def _require_gui_domain(runner) -> str:
    domain = _domain()
    result = _result(runner, ("launchctl", "print", domain), check=False, capture_output=True)
    if result.returncode:
        raise RuntimeError(
            f"macOS GUI launchd domain {domain} is unavailable; OMP Deck requires a logged-in graphical user session"
        )
    return domain


def _loaded(runner) -> bool:
    return (
        _result(
            runner,
            ("launchctl", "print", _target()),
            check=False,
            capture_output=True,
        ).returncode
        == 0
    )


def _bootout(runner) -> None:
    result = _result(runner, ("launchctl", "bootout", _target()), check=False, capture_output=True)
    if result.returncode and _loaded(runner):
        raise RuntimeError("cannot unload the OMP Deck LaunchAgent")
    # bootout acknowledges removal before the process and job have disappeared.
    # Replacing files or bootstrapping sooner races the old instance's teardown.
    deadline = time.monotonic() + 10
    while _loaded(runner):
        if time.monotonic() >= deadline:
            raise RuntimeError("timed out waiting for the OMP Deck LaunchAgent to unload")
        time.sleep(0.05)


def _disabled(runner) -> bool:
    result = _result(
        runner,
        ("launchctl", "print-disabled", _domain()),
        check=False,
        capture_output=True,
    )
    if result.returncode:
        raise RuntimeError("cannot read the OMP Deck launchd enabled state")
    output = result.stdout or ""
    if re.fullmatch(r"\s*disabled services\s*=\s*\{.*\}\s*", output, re.DOTALL) is None:
        raise RuntimeError("launchctl returned an unrecognized enabled-state report")
    quoted_label = f'"{_LABEL}"'
    matching_lines = [line for line in output.splitlines() if quoted_label in line]
    if not matching_lines:
        return False
    if len(matching_lines) != 1:
        raise RuntimeError("launchctl returned an ambiguous OMP Deck enabled state")
    match = re.fullmatch(
        rf'\s*"{re.escape(_LABEL)}"\s*=>\s*(enabled|disabled|true|false)\s*;?\s*',
        matching_lines[0],
        re.IGNORECASE,
    )
    if match is None:
        raise RuntimeError("launchctl returned an unrecognized OMP Deck enabled state")
    return match.group(1).casefold() in {"disabled", "true"}


def _present(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _snapshot_file(path: Path):
    content, mode = _file_snapshot(path)
    if _present(path) and content is None:
        raise RuntimeError(f"cannot snapshot unsafe installation file: {path}")
    return content, mode


def _validate_owned_manifest():
    base_paths = installation_paths()
    root = base_paths.install_root
    try:
        root_info = root.stat(follow_symlinks=False)
        manifest_info = base_paths.manifest_path.stat(follow_symlinks=False)
    except OSError as error:
        raise RuntimeError("OMP Deck installation ownership is missing") from error
    if (
        root.is_symlink()
        or not stat.S_ISDIR(root_info.st_mode)
        or root_info.st_uid != os.geteuid()
        or root_info.st_mode & 0o022
        or not has_ownership_marker(root)
        or base_paths.manifest_path.is_symlink()
        or not stat.S_ISREG(manifest_info.st_mode)
        or manifest_info.st_uid != os.geteuid()
        or manifest_info.st_mode & 0o022
    ):
        raise RuntimeError("OMP Deck installation manifest is not securely owned by the current user")
    return validate_manifest(base_paths)


def _validate_plist(path: Path, runner, *, config_path: Path | None = None) -> dict:
    try:
        info = path.stat(follow_symlinks=False)
    except OSError as error:
        raise RuntimeError("OMP Deck LaunchAgent plist is missing") from error
    if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise RuntimeError("OMP Deck LaunchAgent plist is unsafe")
    lint = _result(runner, ("plutil", "-lint", "--", str(path)), check=False, capture_output=True)
    if lint.returncode:
        raise RuntimeError("OMP Deck LaunchAgent plist is invalid")
    try:
        value = plistlib.loads(path.read_bytes())
    except (OSError, plistlib.InvalidFileException, ValueError) as error:
        raise RuntimeError("OMP Deck LaunchAgent plist is invalid") from error
    if not isinstance(value, dict):
        raise RuntimeError("OMP Deck LaunchAgent plist does not contain a dictionary")
    arguments = value.get("ProgramArguments")
    if (
        value.get("Label") != _LABEL
        or not isinstance(arguments, list)
        or len(arguments) != 6
        or not all(isinstance(item, str) for item in arguments)
        or not Path(arguments[0]).is_absolute()
        or arguments[1:5] != ["-m", "omp_deck", "serve", "--config"]
        or not Path(arguments[5]).is_absolute()
        or (config_path is not None and Path(arguments[5]) != config_path)
    ):
        raise RuntimeError("OMP Deck LaunchAgent plist does not describe the owned service")
    return value


def _validate_existing_service(runner, *, loaded: bool):
    paths = _validate_owned_manifest()
    if not _present(paths.unit_path):
        if loaded:
            raise RuntimeError("loaded OMP Deck LaunchAgent has no owned plist")
        return paths
    _launch_agents_directory(paths.unit_path, create=False)
    _validate_plist(paths.unit_path, runner, config_path=paths.config_path)
    return paths


def _launch_agents_directory(path: Path, *, create: bool = True) -> None:
    home = Path.home()
    library = home / "Library"
    parent = library / "LaunchAgents"
    if path != parent / "com.omp-deck.agent.plist":
        raise RuntimeError(f"unsafe per-user LaunchAgent path: {path}")
    for directory in (library, parent):
        if directory.is_symlink():
            raise RuntimeError(f"unsafe per-user LaunchAgents directory: {directory}")
        if create:
            directory.mkdir(mode=0o755, exist_ok=True)
        info = directory.stat(follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise RuntimeError(f"unsafe per-user LaunchAgents directory: {directory}")
    if parent.resolve() != home.resolve() / "Library" / "LaunchAgents":
        raise RuntimeError(f"unsafe per-user LaunchAgents directory: {parent}")


def _environment(config_path: Path, settings, executable: str) -> dict[str, str]:
    environment = {
        "HOME": str(Path.home().resolve()),
        "OMP_DECK_CONFIG": str(config_path),
    }
    for name in _INHERITED_PATH_ENVIRONMENT:
        value = os.environ.get(name)
        if value:
            environment[name] = str(Path(value).expanduser().absolute())
    for name in _INHERITED_SCALAR_ENVIRONMENT:
        value = os.environ.get(name)
        if value:
            environment[name] = value

    omp_executable = Path(settings.omp_bin).expanduser()
    if not omp_executable.is_absolute():
        resolved_omp = shutil.which(str(omp_executable))
        omp_executable = Path(resolved_omp) if resolved_omp else None
    required_candidates = []
    if omp_executable is not None:
        required_candidates.append(omp_executable.absolute().parent)
    required_candidates.extend(
        [
            Path(executable).expanduser().absolute().parent,
            (Path.home() / ".local" / "bin").absolute(),
        ]
    )
    optional_candidates = [
        Path("/opt/homebrew/bin"),
        Path("/usr/local/bin"),
        Path("/usr/bin"),
        Path("/bin"),
        Path("/usr/sbin"),
        Path("/sbin"),
    ]
    optional_candidates.extend(Path(item) for item in os.environ.get("PATH", "").split(os.pathsep) if item)
    path_entries = []
    for candidate in required_candidates:
        value = str(candidate)
        if value not in path_entries:
            path_entries.append(value)
    for candidate in optional_candidates:
        expanded = candidate.expanduser()
        if expanded.is_absolute() and expanded.is_dir():
            value = str(expanded)
            if value not in path_entries:
                path_entries.append(value)
    environment["PATH"] = os.pathsep.join(path_entries)
    if any("\0" in value for value in environment.values()):
        raise ValueError("launchd environment values cannot contain NUL characters")
    return environment


def launch_agent_plist(
    config_path: Path,
    settings,
    *,
    executable: str = sys.executable,
) -> bytes:
    executable_path = str(Path(executable).expanduser().absolute())
    config_path = Path(config_path).expanduser().absolute()
    value = {
        "Label": _LABEL,
        "ProgramArguments": [
            executable_path,
            "-m",
            "omp_deck",
            "serve",
            "--config",
            str(config_path),
        ],
        "EnvironmentVariables": _environment(config_path, settings, executable_path),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 10,
        "ProcessType": "Background",
        "Umask": 63,
    }
    return plistlib.dumps(value, fmt=plistlib.FMT_XML, sort_keys=False)


def capture_service_state(*, settings=None, config_path=None, runner) -> ServiceSnapshot:
    _require_gui_domain(runner)
    paths = (
        installation_paths(config_file=config_path, state_root=settings.state_root)
        if settings is not None and config_path is not None
        else installation_paths()
    )
    unit, unit_mode = _snapshot_file(paths.unit_path)
    manifest, manifest_mode = _snapshot_file(paths.manifest_path)
    config, config_mode = _snapshot_file(paths.config_path)
    loaded = _loaded(runner)
    if loaded and unit is None:
        raise RuntimeError("cannot snapshot a loaded OMP Deck LaunchAgent without its plist")
    return ServiceSnapshot(
        unit=unit,
        unit_mode=unit_mode,
        manifest=manifest,
        manifest_mode=manifest_mode,
        enabled=not _disabled(runner),
        active=loaded,
        installation=paths,
        roots=_capture_roots(paths),
        config=config,
        config_mode=config_mode,
    )


def restore_service_state(snapshot: ServiceSnapshot, *, runner) -> None:
    domain = _require_gui_domain(runner)
    paths = snapshot.installation or installation_paths()
    loaded = _loaded(runner)
    if _present(paths.unit_path) or loaded:
        _validate_existing_service(runner, loaded=loaded)
    if loaded:
        _bootout(runner)

    _restore_service_files(snapshot, paths)
    if snapshot.unit is not None:
        # A relocation rollback restores the plist's original config argument,
        # which intentionally differs from the failed installation's path.
        _validate_plist(paths.unit_path, runner)
    if snapshot.installation is not None:
        for root in reversed(snapshot.roots):
            _restore_root(root)

    if snapshot.active:
        if snapshot.unit is None:
            raise RuntimeError("cannot restore a loaded LaunchAgent without its plist")
        _result(runner, ("launchctl", "enable", _target()))
        _result(runner, ("launchctl", "bootstrap", domain, str(paths.unit_path)))
        if not snapshot.enabled:
            _result(runner, ("launchctl", "disable", _target()))
    else:
        action = "enable" if snapshot.enabled else "disable"
        _result(runner, ("launchctl", action, _target()))


def install_service(settings, config_path: Path, *, runner, start: bool = True):
    domain = _require_gui_domain(runner)
    base_paths = installation_paths()
    loaded = _loaded(runner)
    replacing = _present(base_paths.unit_path) or loaded
    if replacing:
        _validate_existing_service(runner, loaded=loaded)

    intended_paths = installation_paths(config_file=config_path, state_root=settings.state_root)
    validate_ownership_claim(intended_paths.install_root)
    validate_ownership_claim(
        intended_paths.config_root,
        (intended_paths.config_path,),
    )
    validate_ownership_claim(
        intended_paths.state_root,
        (intended_paths.state_root / "auth.json", intended_paths.state_root / ".auth.lock"),
    )
    _launch_agents_directory(intended_paths.unit_path)
    if loaded:
        _bootout(runner)
    if replacing:
        base_paths.unit_path.unlink(missing_ok=True)

    paths = prepare_installation(settings, config_path)
    write_atomic(paths.unit_path, launch_agent_plist(paths.config_path, settings), 0o644)
    _validate_plist(paths.unit_path, runner, config_path=paths.config_path)
    _result(runner, ("launchctl", "enable", _target()))
    if start:
        _result(runner, ("launchctl", "bootstrap", domain, str(paths.unit_path)))
    return paths


def service_status(*, runner) -> int:
    _require_gui_domain(runner)
    loaded = _loaded(runner)
    print(f"OMP Deck LaunchAgent: {'loaded' if loaded else 'not loaded'}")
    return 0 if loaded else 1


def restart_service(*, runner) -> None:
    _require_gui_domain(runner)
    loaded = _loaded(runner)
    if not loaded:
        raise RuntimeError("OMP Deck LaunchAgent is not loaded")
    _validate_existing_service(runner, loaded=True)
    _result(runner, ("launchctl", "kickstart", "-k", _target()))


def remove_service(*, runner):
    _require_gui_domain(runner)
    loaded = _loaded(runner)
    paths = _validate_existing_service(runner, loaded=loaded)
    if loaded:
        _bootout(runner)
    paths.unit_path.unlink(missing_ok=True)
    _result(runner, ("launchctl", "enable", _target()))
    return paths
