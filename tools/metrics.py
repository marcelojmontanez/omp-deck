#!/usr/bin/env python3
import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_SUFFIXES = {".py", ".js", ".ts", ".css"}
EXCLUDED_PARTS = {".git", "__pycache__"}


def maintained_sources():
    """Return tracked and non-ignored source files from the current checkout."""
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode == 0:
        names = (name for name in result.stdout.decode("utf-8").split("\0") if name)
        return tuple(name for name in names if Path(name).suffix.lower() in SOURCE_SUFFIXES and (ROOT / name).is_file())
    return tuple(
        str(path.relative_to(ROOT))
        for path in sorted(ROOT.rglob("*"))
        if path.is_file()
        and path.suffix.lower() in SOURCE_SUFFIXES
        and not EXCLUDED_PARTS.intersection(path.relative_to(ROOT).parts)
    )


MAINTAINED_SOURCES = maintained_sources()
SOURCE_FILES = tuple(ROOT / path for path in MAINTAINED_SOURCES if not Path(path).name.startswith("test_"))
MAX_FILE_LINES = 500
MAX_PYTHON_COMPLEXITY = 25
BRANCH_NODES = (
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.Try,
    ast.BoolOp,
    ast.IfExp,
    ast.Match,
)


def python_complexities(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            branches = sum(isinstance(child, BRANCH_NODES) for child in ast.walk(node))
            yield node.name, node.lineno, 1 + branches


def main():
    failures = []
    for path in SOURCE_FILES:
        lines = len(path.read_text(encoding="utf-8").splitlines())
        relative = path.relative_to(ROOT)
        print(f"{relative}: {lines} lines")
        if lines > MAX_FILE_LINES:
            failures.append(f"{relative}: {lines} lines exceeds {MAX_FILE_LINES}")
        if path.suffix == ".py":
            complexities = list(python_complexities(path))
            if complexities:
                name, line, complexity = max(complexities, key=lambda item: item[2])
                print(f"  max complexity: {complexity} ({name}:{line})")
            for name, line, complexity in complexities:
                if complexity > MAX_PYTHON_COMPLEXITY:
                    failures.append(
                        f"{relative}:{line} {name}: complexity {complexity} exceeds {MAX_PYTHON_COMPLEXITY}"
                    )
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
