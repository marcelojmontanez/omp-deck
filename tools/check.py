#!/usr/bin/env python3
import os
import subprocess
import sys
from pathlib import Path
from metrics import MAINTAINED_SOURCES

ROOT = Path(__file__).resolve().parents[1]
NODE_BIN = os.environ.get("NODE_BIN", "node")
EXCLUDED_PARTS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
}
TEXT_SUFFIXES = {
    ".css",
    ".html",
    ".js",
    ".json",
    ".py",
    ".service",
    ".toml",
    ".ts",
    ".txt",
    ".yml",
    ".yaml",
}
TEXT_NAMES = {".editorconfig", ".gitignore", "pre-commit", "commit-msg"}


def project_text_files():
    for path in sorted(ROOT.rglob("*")):
        if (
            not path.is_file()
            or EXCLUDED_PARTS.intersection(path.parts)
            or any(part.endswith(".egg-info") for part in path.parts)
        ):
            continue
        if path.suffix in TEXT_SUFFIXES or path.name in TEXT_NAMES:
            yield path


def check_text_files():
    failures = []
    for path in project_text_files():
        data = path.read_bytes()
        relative = path.relative_to(ROOT)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            failures.append(f"{relative}: not UTF-8")
            continue
        if b"\r" in data:
            failures.append(f"{relative}: contains CRLF line endings")
        if data and not data.endswith(b"\n"):
            failures.append(f"{relative}: missing final newline")
        for number, line in enumerate(text.splitlines(), 1):
            if line.rstrip(" \t") != line:
                failures.append(f"{relative}:{number}: trailing whitespace")
    return failures


def run(command):
    python_path = str(ROOT / "src")
    if os.environ.get("PYTHONPATH"):
        python_path += os.pathsep + os.environ["PYTHONPATH"]
    environment = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": python_path,
    }
    return subprocess.run(command, cwd=ROOT, env=environment).returncode


def check_javascript():
    for path in MAINTAINED_SOURCES:
        if Path(path).suffix == ".js" and run([NODE_BIN, "--check", path]):
            return 1
    return 0


def main():
    failures = check_text_files()
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    python_sources = [
        path for path in MAINTAINED_SOURCES if Path(path).suffix == ".py"
    ]
    checks = [
        [sys.executable, "-m", "py_compile", *python_sources],
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."],
        [sys.executable, "tools/metrics.py"],
    ]
    for command in checks:
        if run(command):
            return 1
    return check_javascript()


if __name__ == "__main__":
    raise SystemExit(main())
