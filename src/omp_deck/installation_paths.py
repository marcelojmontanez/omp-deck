import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path


MANIFEST_NAME = ".install-manifest.json"
OWNERSHIP_MARKER = ".omp-deck-owned"
MANIFEST_VERSION = 3


@dataclass(frozen=True)
class InstallationPaths:
    install_root: Path
    unit_path: Path
    config_root: Path
    config_path: Path
    state_root: Path

    @property
    def manifest_path(self):
        return self.install_root / MANIFEST_NAME


def absolute_path(value):
    return Path(os.path.abspath(Path(value).expanduser()))



def installation_paths(*, config_file=None, state_root=None):
    home = absolute_path(Path.home())
    data_home = absolute_path(os.environ.get("XDG_DATA_HOME", home / ".local/share"))
    config_home = absolute_path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
    state_home = absolute_path(os.environ.get("XDG_STATE_HOME", home / ".local/state"))
    config_path = (
        absolute_path(config_file)
        if config_file is not None
        else config_home / "omp-deck/config.json"
    )
    return InstallationPaths(
        install_root=data_home / "omp-deck",
        unit_path=config_home / "systemd/user/omp-deck.service",
        config_root=config_path.parent,
        config_path=config_path,
        state_root=(
            absolute_path(state_root)
            if state_root is not None
            else state_home / "omp-deck"
        ),
    )


def manifest_content(paths):
    return json.dumps(
        {
            "version": MANIFEST_VERSION,
            "install_root": str(paths.install_root),
            "unit_path": str(paths.unit_path),
            "config_root": str(paths.config_root),
            "config_path": str(paths.config_path),
            "state_root": str(paths.state_root),
        },
        indent=2,
        sort_keys=True,
    ) + "\n"


def validate_manifest(paths):
    try:
        value = json.loads(paths.manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise RuntimeError("OMP Deck installation manifest is missing") from error
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"cannot read installation manifest: {error}") from error
    required = {
        "version",
        "install_root",
        "unit_path",
        "config_root",
        "config_path",
        "state_root",
    }
    if not isinstance(value, dict) or set(value) != required or value["version"] != MANIFEST_VERSION:
        raise RuntimeError("invalid OMP Deck installation manifest")
    fixed = ("install_root", "unit_path")
    if any(value[name] != str(getattr(paths, name)) for name in fixed):
        raise RuntimeError("installation manifest does not match approved OMP Deck paths")
    recorded = {}
    for name in required - {"version"}:
        item = value[name]
        if not isinstance(item, str) or not Path(item).is_absolute():
            raise RuntimeError("installation manifest contains an invalid path")
        recorded[name] = Path(item)
    if recorded["config_path"].parent != recorded["config_root"]:
        raise RuntimeError("installation manifest contains an invalid config path")
    return InstallationPaths(**recorded)


def mark_owned(root, allowed_existing=()):
    if root.is_symlink():
        raise RuntimeError(f"refusing to mark symlink as owned: {root}")
    if root.exists():
        if not root.is_dir():
            raise RuntimeError(f"owned path is not a directory: {root}")
        if has_ownership_marker(root):
            return
        allowed = {Path(path).absolute() for path in allowed_existing}
        entries = list(root.iterdir())
        unexpected = [path for path in entries if path.absolute() not in allowed]
        if unexpected or any(path.is_symlink() or not path.is_file() for path in entries):
            raise RuntimeError(f"refusing to claim non-empty unowned path: {root}")
    else:
        root.mkdir(parents=True)
    marker = root / OWNERSHIP_MARKER
    marker.write_text("OMP Deck owned directory\n", encoding="utf-8")


def has_ownership_marker(root):
    marker = root / OWNERSHIP_MARKER
    if marker.is_symlink():
        return False
    try:
        return marker.is_file() and marker.read_text(encoding="utf-8") == "OMP Deck owned directory\n"
    except OSError:
        return False


def remove_owned_root(root, approved_roots):
    root = root.expanduser()
    approved = {path.expanduser().absolute() for path in approved_roots}
    absolute = root.absolute()
    if absolute not in approved:
        raise RuntimeError(f"refusing to purge unapproved path: {root}")
    home = Path.home().resolve()
    forbidden = {
        Path("/"),
        home,
        Path(os.environ.get("XDG_CONFIG_HOME", home / ".config")).expanduser().resolve(),
        Path(os.environ.get("XDG_DATA_HOME", home / ".local/share")).expanduser().resolve(),
        Path(os.environ.get("XDG_STATE_HOME", home / ".local/state")).expanduser().resolve(),
    }
    if root.is_symlink():
        raise RuntimeError(f"refusing to purge symlink: {root}")
    try:
        resolved = root.resolve(strict=True)
    except FileNotFoundError:
        return
    if resolved in forbidden or len(resolved.parts) < 4:
        raise RuntimeError(f"refusing to purge unsafe path: {root}")
    if not resolved.is_dir() or not has_ownership_marker(resolved):
        raise RuntimeError(f"refusing to purge unowned path: {root}")
    shutil.rmtree(resolved)
