"""PEP 517 backend that makes package identity an artifact invariant."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from email.parser import BytesParser
from pathlib import Path

from packaging.version import Version
from setuptools import build_meta as _setuptools_backend

_ROOT = Path(__file__).resolve().parent
_IDENTITY_FILE = _ROOT / "src" / "omp_deck" / "_build_identity.py"
_PACKAGE_ROOT = _ROOT / "src" / "omp_deck"
_SOURCE_MANIFEST = _ROOT / "_source_manifest.json"
_FULL_REVISION = re.compile(r"[0-9a-f]{40}")
_VERSION_BASELINE_REVISION = "fe1754b15098937b6bb82610a782782dcf58bfa6"
_VERSION_FAMILY = "1.5"
_FORBIDDEN_OVERRIDE_PREFIXES = (
    "SETUPTOOLS_SCM_PRETEND_",
    "SETUPTOOLS_SCM_OVERRIDES_FOR_OMP_DECK",
)
_ROOT_SOURCE_INPUTS = {"LICENSE", "MANIFEST.in", "README.md", "_build_backend.py", "pyproject.toml"}


class BuildIdentityError(RuntimeError):
    """Raised when an immutable artifact identity cannot be established."""


def _git(*arguments: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(_ROOT), *arguments],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as error:
        raise BuildIdentityError("git is required to identify a source checkout") from error
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip() or "git command failed"
        raise BuildIdentityError(detail)
    return completed.stdout.strip()


def _repository_root() -> Path | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(_ROOT), "rev-parse", "--show-toplevel"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if completed.returncode:
        return None
    root = Path(completed.stdout.strip()).resolve()
    return root if root == _ROOT else None


def _reject_identity_overrides() -> None:
    forbidden = sorted(
        name for name in os.environ if any(name.startswith(prefix) for prefix in _FORBIDDEN_OVERRIDE_PREFIXES)
    )
    if forbidden:
        raise BuildIdentityError(f"artifact identity overrides are forbidden: {', '.join(forbidden)}")


def _file_digest(path: Path, root: Path = _ROOT) -> str:
    relative = path.relative_to(root)
    if any((root / part).is_symlink() for part in (relative, *relative.parents)):
        # Packaging dereferences links; hashing only their names would permit mutable external code.
        raise BuildIdentityError(f"artifact source must not use symlinks: {relative}")
    if path.is_file():
        payload = b"file\0" + path.read_bytes()
    else:
        raise BuildIdentityError(f"tracked source is not a file: {path.relative_to(_ROOT)}")
    return hashlib.sha256(payload).hexdigest()


def _tracked_files() -> set[str]:
    return {name for name in _git("ls-files", "--cached", "-z").split("\0") if name}


def _excluded_package_path(relative: Path) -> bool:
    return (
        relative.name == "_build_identity.py"
        or relative.suffix == ".pyc"
        or bool({"__pycache__", "node_modules"}.intersection(relative.parts))
    )


def _package_files() -> set[str]:
    files: set[str] = set()
    for directory, children, names in os.walk(_PACKAGE_ROOT, followlinks=False):
        children[:] = [name for name in children if name not in {"__pycache__", "node_modules"}]
        parent = Path(directory)
        if any((parent / name).is_symlink() for name in children):
            raise BuildIdentityError("artifact package directories must not use symlinks")
        for name in names:
            path = parent / name
            if not _excluded_package_path(path.relative_to(_PACKAGE_ROOT)):
                files.add(path.relative_to(_ROOT).as_posix())
    return files


def _source_snapshot(revision: str) -> bytes:
    tracked = _tracked_files()
    missing_root_inputs = sorted(_ROOT_SOURCE_INPUTS - tracked)
    if missing_root_inputs:
        raise BuildIdentityError("artifact build inputs must be committed: " + ", ".join(missing_root_inputs))
    untracked_package_files = sorted(_package_files() - tracked)
    if untracked_package_files:
        raise BuildIdentityError(
            "artifact builds reject untracked or ignored package files: " + ", ".join(untracked_package_files)
        )
    release_inputs = {name for name in tracked if name in _ROOT_SOURCE_INPUTS or name.startswith("src/omp_deck/")}
    files = {name: _file_digest(_ROOT / name) for name in sorted(release_inputs)}
    return (
        json.dumps(
            {"schema": 1, "revision": revision, "files": files},
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()


def _verify_committed_snapshot(staged: Path, revision: str) -> None:
    # Git status can hide edits behind assume-unchanged or skip-worktree index flags.
    for record in _git("ls-tree", "-r", "-z", revision).split("\0"):
        if not record:
            continue
        attributes, name = record.split("\t", 1)
        _, kind, expected = attributes.split()
        if kind != "blob":
            raise BuildIdentityError(f"unsupported committed source object: {name}")
        content = (staged / name).read_bytes()
        actual = hashlib.sha1(f"blob {len(content)}\0".encode() + content, usedforsecurity=False).hexdigest()
        if actual != expected:
            raise BuildIdentityError(f"artifact source differs from committed revision: {name}")


def _verify_archive_snapshot(revision: str, expected_digest: str) -> None:
    try:
        content = _SOURCE_MANIFEST.read_bytes()
        manifest = json.loads(content)
    except (OSError, ValueError) as error:
        raise BuildIdentityError("source distribution content manifest is missing or invalid") from error
    if hashlib.sha256(content).hexdigest() != expected_digest:
        raise BuildIdentityError("source distribution content manifest does not match its build identity")
    if manifest.get("schema") != 1 or manifest.get("revision") != revision:
        raise BuildIdentityError("source distribution content manifest has ambiguous source identity")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise BuildIdentityError("source distribution content manifest has no source files")
    for name, expected in files.items():
        if not isinstance(name, str) or not isinstance(expected, str):
            raise BuildIdentityError("source distribution content manifest has invalid entries")
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise BuildIdentityError("source distribution content manifest has unsafe paths")
        path = _ROOT / relative
        if not path.exists() or _file_digest(path) != expected:
            raise BuildIdentityError(f"source distribution content changed: {name}")
    extra_package_files = sorted(_package_files() - set(files))
    if extra_package_files:
        raise BuildIdentityError(
            "source distribution contains files outside its immutable snapshot: " + ", ".join(extra_package_files)
        )


def _archive_identity() -> tuple[str, str]:
    package_info = _ROOT / "PKG-INFO"
    if not package_info.is_file() or not _IDENTITY_FILE.is_file():
        raise BuildIdentityError(
            "cannot build an identified artifact without a complete Git checkout or an omp-deck source distribution"
        )
    metadata = BytesParser().parsebytes(package_info.read_bytes())
    if metadata.get("Name") != "omp-deck" or not metadata.get("Version"):
        raise BuildIdentityError("source distribution has missing or ambiguous package metadata")

    assignments: dict[str, str] = {}
    for statement in ast.parse(_IDENTITY_FILE.read_text(encoding="utf-8")).body:
        if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
            continue
        target = statement.targets[0]
        if isinstance(target, ast.Name) and target.id in {"VERSION", "REVISION", "SOURCE_MANIFEST_SHA256"}:
            value = ast.literal_eval(statement.value)
            if isinstance(value, str):
                assignments[target.id] = value
    version = assignments.get("VERSION", "")
    revision = assignments.get("REVISION", "")
    manifest_digest = assignments.get("SOURCE_MANIFEST_SHA256", "")
    if version != metadata["Version"] or not _FULL_REVISION.fullmatch(revision):
        raise BuildIdentityError("source distribution build identity does not match its package metadata")
    if not re.fullmatch(r"[0-9a-f]{64}", manifest_digest):
        raise BuildIdentityError("source distribution build identity has no content manifest")
    _verify_archive_snapshot(revision, manifest_digest)
    return version, revision


def _scm_identity() -> tuple[str, str]:
    if _git("rev-parse", "--is-shallow-repository") != "false":
        raise BuildIdentityError("artifact builds require complete Git history; shallow checkout detected")
    status = _git("status", "--porcelain=v1", "--untracked-files=all")
    if status:
        raise BuildIdentityError(f"artifact builds require a clean checkout; changed paths:\n{status}")
    revision = _git("rev-parse", "--verify", "HEAD").lower()
    if not _FULL_REVISION.fullmatch(revision):
        raise BuildIdentityError("Git did not provide an unambiguous full HEAD revision")
    try:
        _git("merge-base", "--is-ancestor", _VERSION_BASELINE_REVISION, "HEAD")
        distance = int(_git("rev-list", "--count", f"{_VERSION_BASELINE_REVISION}..HEAD"))
    except (BuildIdentityError, ValueError) as error:
        raise BuildIdentityError(
            f"version baseline {_VERSION_BASELINE_REVISION} is missing or not an ancestor of HEAD"
        ) from error
    version = Version(f"{_VERSION_FAMILY}.{distance}+g{revision}")
    return str(version), revision


def _write_identity(root: Path, version: str, revision: str, manifest_digest: str) -> None:
    (root / "src" / "omp_deck" / "_build_identity.py").write_text(
        "# Generated while constructing an artifact; do not edit or commit.\n"
        f"VERSION = {version!r}\n"
        f"REVISION = {revision!r}\n"
        f"SOURCE_MANIFEST_SHA256 = {manifest_digest!r}\n",
        encoding="utf-8",
    )


def _run_identified(hook, *args, **kwargs):
    _reject_identity_overrides()
    generated = _repository_root() is not None
    version, revision = _scm_identity() if generated else _archive_identity()
    snapshot = _source_snapshot(revision) if generated else _SOURCE_MANIFEST.read_bytes()
    manifest = json.loads(snapshot)
    inputs = _tracked_files() if generated else set(manifest["files"]) | {"PKG-INFO"}
    previous_directory = Path.cwd()
    # Never reuse build/lib or egg-info: setuptools can otherwise ship removed or stale modules.
    with tempfile.TemporaryDirectory(prefix="omp-deck-build-") as temporary:
        staged = Path(temporary).resolve()
        for name in inputs:
            source = _ROOT / name
            digest = _file_digest(source)
            destination = staged / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            if _file_digest(destination, staged) != digest:
                raise BuildIdentityError(f"source changed while staging: {name}")
        for name, expected in manifest["files"].items():
            if _file_digest(staged / name, staged) != expected:
                raise BuildIdentityError(f"source changed while staging: {name}")
        if generated and _scm_identity() != (version, revision):
            raise BuildIdentityError("source revision changed while staging")
        if generated:
            _verify_committed_snapshot(staged, revision)
        if kwargs.get("metadata_directory") is not None:
            metadata_source = Path(kwargs["metadata_directory"])
            prepared = staged / ".prepared-metadata" / metadata_source.name
            try:
                shutil.copytree(metadata_source, prepared)
                metadata = BytesParser().parsebytes((prepared / "METADATA").read_bytes())
            except OSError as error:
                raise BuildIdentityError("prepared wheel metadata is missing or invalid") from error
            if metadata.get("Name") != "omp-deck" or metadata.get("Version") != version:
                raise BuildIdentityError("prepared wheel metadata does not match the source revision")
            kwargs["metadata_directory"] = str(prepared)
        (staged / "_source_manifest.json").write_bytes(snapshot)
        _write_identity(staged, version, revision, hashlib.sha256(snapshot).hexdigest())
        try:
            os.chdir(staged)
            return hook(*args, **kwargs)
        finally:
            os.chdir(previous_directory)


def build_sdist(sdist_directory, config_settings=None):
    return _run_identified(_setuptools_backend.build_sdist, str(Path(sdist_directory).resolve()), config_settings)


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    return _run_identified(
        _setuptools_backend.build_wheel,
        str(Path(wheel_directory).resolve()),
        config_settings,
        metadata_directory=str(Path(metadata_directory).resolve()) if metadata_directory is not None else None,
    )


def prepare_metadata_for_build_wheel(metadata_directory, config_settings=None):
    return _run_identified(
        _setuptools_backend.prepare_metadata_for_build_wheel,
        str(Path(metadata_directory).resolve()),
        config_settings,
    )


def build_editable(wheel_directory, config_settings=None, metadata_directory=None):
    raise BuildIdentityError(
        "editable installs cannot provide an immutable source revision; run from the checkout or build a wheel"
    )


def prepare_metadata_for_build_editable(metadata_directory, config_settings=None):
    raise BuildIdentityError(
        "editable installs cannot provide an immutable source revision; run from the checkout or build a wheel"
    )


def get_requires_for_build_sdist(config_settings=None):
    return _run_identified(_setuptools_backend.get_requires_for_build_sdist, config_settings)


def get_requires_for_build_wheel(config_settings=None):
    return _run_identified(_setuptools_backend.get_requires_for_build_wheel, config_settings)


def get_requires_for_build_editable(config_settings=None):
    raise BuildIdentityError(
        "editable installs cannot provide an immutable source revision; run from the checkout or build a wheel"
    )
