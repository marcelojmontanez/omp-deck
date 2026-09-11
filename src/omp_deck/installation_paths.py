import json
import os
import shutil
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from ._platform_paths import (
    config_home,
    data_home,
    default_config_path,
    default_install_root,
    default_state_root,
    state_home,
)

if os.name == "nt":
    from ._windows_security import is_private_path, owned_by_current_user, secure_created_path, secure_path

MANIFEST_NAME = ".install-manifest.json"
OWNERSHIP_MARKER = ".omp-deck-owned"
MANIFEST_VERSION = 3
_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400


def _is_link_like(path):
    try:
        info = path.lstat()
    except OSError:
        return False
    return path.is_symlink() or bool(getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT)


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
    if os.name == "nt":
        config_path = absolute_path(config_file) if config_file is not None else default_config_path()
        install_root = default_install_root()
        return InstallationPaths(
            install_root=install_root,
            unit_path=install_root / "omp-deck-task.xml",
            config_root=config_path.parent,
            config_path=config_path,
            state_root=absolute_path(state_root) if state_root is not None else default_state_root(),
        )
    config_root = absolute_path(config_home())
    install_root = absolute_path(default_install_root())
    state_root_path = absolute_path(state_home())
    config_path = absolute_path(config_file) if config_file is not None else config_root / "omp-deck/config.json"
    return InstallationPaths(
        install_root=install_root,
        unit_path=(
            Path.home() / "Library/LaunchAgents/com.omp-deck.agent.plist"
            if sys.platform == "darwin"
            else config_root / "systemd/user/omp-deck.service"
        ),
        config_root=config_path.parent,
        config_path=config_path,
        state_root=absolute_path(state_root) if state_root is not None else state_root_path / "omp-deck",
    )


def manifest_content(paths):
    return (
        json.dumps(
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
        )
        + "\n"
    )


def validate_manifest(paths):
    if os.name == "nt" and not is_private_path(paths.manifest_path):
        raise RuntimeError("OMP Deck installation manifest is unsafe")
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


def validate_ownership_claim(root, allowed_existing=()):
    if _is_link_like(root):
        raise RuntimeError(f"refusing to mark symlink or reparse point as owned: {root}")
    if not root.exists():
        return
    info = root.stat(follow_symlinks=False)
    owned = owned_by_current_user(root) if os.name == "nt" else info.st_uid == os.geteuid()
    if not stat.S_ISDIR(info.st_mode) or not owned:
        raise RuntimeError(f"owned path is unsafe: {root}")
    if has_ownership_marker(root):
        return
    entries = list(root.iterdir())
    allowed = {Path(path).absolute() for path in allowed_existing}
    unexpected = [path for path in entries if path.absolute() not in allowed]
    unsupported = [
        path
        for path in entries
        if _is_link_like(path)
        or not (
            path.is_file()
            or (os.name == "nt" and path.is_dir() and owned_by_current_user(path) and is_private_path(path))
        )
    ]
    if unexpected or unsupported:
        raise RuntimeError(f"refusing to claim non-empty unowned path: {root}")


def mark_owned(root, allowed_existing=(), *, mode=None):
    validate_ownership_claim(root, allowed_existing)
    if _is_link_like(root):
        raise RuntimeError(f"refusing to mark symlink or reparse point as owned: {root}")
    created = False
    if root.exists():
        info = root.stat(follow_symlinks=False)
        owned = owned_by_current_user(root) if os.name == "nt" else info.st_uid == os.geteuid()
        if not stat.S_ISDIR(info.st_mode) or not owned:
            raise RuntimeError(f"owned path is unsafe: {root}")
        if has_ownership_marker(root):
            if os.name == "nt":
                secure_path(root)
            elif mode is not None:
                root.chmod(mode)
            return
        entries = list(root.iterdir())
        allowed = {Path(path).absolute() for path in allowed_existing}
        unexpected = [path for path in entries if path.absolute() not in allowed]
        unsupported = [
            path
            for path in entries
            if _is_link_like(path)
            or not (
                path.is_file()
                or (os.name == "nt" and path.is_dir() and owned_by_current_user(path) and is_private_path(path))
            )
        ]
        if unexpected or unsupported:
            raise RuntimeError(f"refusing to claim non-empty unowned path: {root}")
    else:
        root.mkdir(parents=True)
        created = True
    if os.name == "nt":
        if created:
            secure_created_path(root)
        else:
            secure_path(root)
    marker = root / OWNERSHIP_MARKER
    with marker.open("x", encoding="utf-8", newline="") as stream:
        stream.write("OMP Deck owned directory\n")
    if os.name == "nt":
        secure_created_path(marker)

    if os.name != "nt" and mode is not None:
        root.chmod(mode)


def has_ownership_marker(root):
    marker = root / OWNERSHIP_MARKER
    if _is_link_like(marker):
        return False
    try:
        return marker.is_file() and marker.read_text(encoding="utf-8") == "OMP Deck owned directory\n"
    except OSError:
        return False


def validate_owned_root(root, approved_roots):
    root = root.expanduser()
    approved = {path.expanduser().absolute() for path in approved_roots}
    absolute = root.absolute()
    if absolute not in approved:
        raise RuntimeError(f"refusing to purge unapproved path: {root}")
    home = Path.home().resolve()
    forbidden = {
        Path(home.anchor),
        home,
        absolute_path(config_home()).resolve(),
        absolute_path(data_home()).resolve(),
        absolute_path(state_home()).resolve(),
    }
    if _is_link_like(root):
        raise RuntimeError(f"refusing to purge symlink or reparse point: {root}")
    try:
        resolved = root.resolve(strict=True)
    except FileNotFoundError:
        return None
    if resolved in forbidden or len(resolved.parts) < 4:
        raise RuntimeError(f"refusing to purge unsafe path: {root}")
    if os.name == "nt" and (not owned_by_current_user(resolved) or not is_private_path(resolved)):
        raise RuntimeError(f"refusing to purge unsafe path: {root}")
    if not resolved.is_dir() or not has_ownership_marker(resolved):
        raise RuntimeError(f"refusing to purge unowned path: {root}")
    return resolved


def remove_owned_root(root, approved_roots):
    resolved = validate_owned_root(root, approved_roots)
    if resolved is not None:
        shutil.rmtree(resolved)
