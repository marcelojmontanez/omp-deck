import os
from pathlib import Path

PROJECT_MARKERS = {
    ".git",
    "AGENTS.md",
    "Cargo.toml",
    "go.mod",
    "package.json",
    "pyproject.toml",
}
IGNORED_PROJECT_DIRS = {
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "target",
    "vendor",
}


def _resolved_project_root(root):
    root = Path(root).expanduser()
    if root.is_symlink():
        raise ValueError("invalid project root")
    try:
        resolved = root.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ValueError("invalid project root") from error
    if not resolved.is_dir():
        raise ValueError("invalid project root")
    return resolved


def _reject_symlink_components(root, relative, *, allow_missing_leaf):
    current = root
    for index, part in enumerate(relative.parts):
        current = current / part
        if current.is_symlink():
            raise ValueError("invalid project")
        if not current.exists():
            if allow_missing_leaf and index == len(relative.parts) - 1:
                return
            raise ValueError("invalid project")
        if not current.is_dir():
            raise ValueError("invalid project")


def resolve_project_path(root, project, *, must_exist=True):
    if not isinstance(project, str):
        raise ValueError("invalid project")
    parts = project.split("/")
    if (
        not project
        or len(project) > 240
        or project != project.strip()
        or "\\" in project
        or any(ord(character) < 32 or ord(character) == 127 for character in project)
        or any(not part or part in {".", ".."} or part.startswith(".") for part in parts)
    ):
        raise ValueError("invalid project")
    relative = Path(*parts)
    if relative.is_absolute() or relative.as_posix() != project:
        raise ValueError("invalid project")
    resolved_root = _resolved_project_root(root)
    _reject_symlink_components(resolved_root, relative, allow_missing_leaf=not must_exist)
    try:
        candidate = (resolved_root / relative).resolve(strict=must_exist)
        candidate.relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError) as error:
        raise ValueError("invalid project") from error
    if must_exist and not candidate.is_dir():
        raise ValueError("invalid project")
    return candidate


def create_project_directory(root, project):
    candidate = resolve_project_path(root, project, must_exist=False)
    resolved_root = _resolved_project_root(root)
    relative = candidate.relative_to(resolved_root)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(resolved_root, flags)
    try:
        for part in relative.parts[:-1]:
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        os.mkdir(relative.name, mode=0o755, dir_fd=descriptor)
    except FileExistsError as error:
        raise ValueError("project already exists") from error
    except OSError as error:
        raise RuntimeError("could not create project") from error
    finally:
        os.close(descriptor)
    return resolve_project_path(resolved_root, relative.as_posix())


def is_launchable_directory(relative, entries):
    return len(relative.parts) <= 2 or bool(PROJECT_MARKERS.intersection(entries))


def list_launchable_projects(root, *, max_depth=2):
    if type(max_depth) is not int or not 1 <= max_depth <= 8:
        raise ValueError("max_depth must be an integer between 1 and 8")
    try:
        resolved_root = _resolved_project_root(root)
    except ValueError:
        return []
    projects = []
    for current, directories, files in os.walk(resolved_root, followlinks=False):
        current_path = Path(current)
        relative = current_path.relative_to(resolved_root)
        entries = set(directories) | set(files)
        if len(relative.parts) >= max_depth:
            directories.clear()
        else:
            safe_directories = []
            for name in directories:
                if name.startswith(".") or name in IGNORED_PROJECT_DIRS:
                    continue
                child = current_path / name
                try:
                    if child.is_symlink() or not child.is_dir():
                        continue
                    child.resolve(strict=True).relative_to(resolved_root)
                except (OSError, RuntimeError, ValueError):
                    continue
                safe_directories.append(name)
            directories[:] = safe_directories
        if relative.parts and is_launchable_directory(relative, entries):
            projects.append(relative.as_posix())
    return sorted(projects, key=str.casefold)
