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


def resolve_project_path(root, project, *, must_exist=True):
    if not isinstance(project, str):
        raise ValueError("invalid project")
    project = project.strip()
    relative = Path(project)
    if (
        not project
        or len(project) > 240
        or relative.is_absolute()
        or any(part in {"", ".", ".."} or part.startswith(".") for part in relative.parts)
    ):
        raise ValueError("invalid project")
    try:
        resolved_root = root.resolve(strict=True)
        candidate = (resolved_root / relative).resolve(strict=must_exist)
        candidate.relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError) as error:
        raise ValueError("invalid project") from error
    return candidate


def is_launchable_directory(relative, entries):
    return len(relative.parts) <= 2 or bool(PROJECT_MARKERS.intersection(entries))


def list_launchable_projects(root):
    try:
        resolved_root = root.resolve(strict=True)
    except (OSError, RuntimeError):
        return []
    projects = []
    for current, directories, files in os.walk(resolved_root, followlinks=False):
        current_path = Path(current)
        relative = current_path.relative_to(resolved_root)
        entries = set(directories) | set(files)
        safe_directories = []
        for name in directories:
            if name.startswith(".") or name in IGNORED_PROJECT_DIRS:
                continue
            try:
                (current_path / name).resolve(strict=True).relative_to(resolved_root)
            except (OSError, RuntimeError, ValueError):
                continue
            safe_directories.append(name)
        directories[:] = safe_directories
        if relative.parts and is_launchable_directory(relative, entries):
            projects.append(relative.as_posix())
    return sorted(projects, key=str.casefold)
