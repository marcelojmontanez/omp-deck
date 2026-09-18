"""Evidence for the exact checkout checked by the existing repository gate."""

from __future__ import annotations

import hashlib
import os
import platform
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def git(root: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", *args], cwd=root, stderr=subprocess.DEVNULL, timeout=30)


def fingerprint(root: Path) -> dict | None:
    """No reuse across edited bytes, staging, tools, dependencies, or environments."""
    try:
        records = git(root, "ls-files", "--stage", "-z").split(b"\0")
        digest = hashlib.sha256()
        for record in records:
            if not record:
                continue
            meta, name = record.split(b"\t", 1)
            mode, expected, stage = meta.split()
            path = root / os.fsdecode(name)
            if stage != b"0" or mode not in {b"100644", b"100755", b"120000"}:
                return None
            content = os.fsencode(os.readlink(path)) if path.is_symlink() else path.read_bytes()
            actual = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest().encode()
            if actual != expected:
                return None
            digest.update(record + b"\0")
        if git(root, "ls-files", "--others", "--exclude-standard", "-z"):
            return None
        tools = []
        metadata = "import importlib.metadata as m,json;print(json.dumps(sorted((d.metadata['Name'],d.version,d.read_text('direct_url.json')) for d in m.distributions())))"
        for argv in (
            [sys.executable, "-c", metadata],
            [sys.executable, "-m", "ruff", "--version"],
            [os.environ.get("NODE_BIN", "node"), "--version"],
            [os.environ.get("NPM_BIN", "npm.cmd" if os.name == "nt" else "npm"), "--version"],
        ):
            result = subprocess.run(argv, capture_output=True, timeout=30, check=True)
            tools.append(hashlib.sha256(result.stdout).hexdigest())
        return {
            "source": digest.hexdigest(),
            "python": sys.version,
            "executable": sys.executable,
            "platform": platform.platform(),
            "tools": tools,
        }
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def receipt_path(root: Path) -> Path:
    git_dir = Path(os.fsdecode(git(root, "rev-parse", "--absolute-git-dir")).strip())
    return git_dir / "omp-deck-release" / "check.json"


def record(root: Path, before: dict | None, *, release: bool) -> None:
    if before is None or fingerprint(root) != before:
        return
    from .storage import directory, save

    path = receipt_path(root)
    directory(path.parent)
    save(path, {"version": 1, "fingerprint": before, "release": release})


def valid(root: Path, *, release: bool = False) -> bool:
    from .storage import load

    try:
        receipt = load(receipt_path(root))
        current = fingerprint(root)
        return (
            current is not None
            and receipt.get("fingerprint") == current
            and (not release or receipt.get("release") is True)
        )
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        return False
