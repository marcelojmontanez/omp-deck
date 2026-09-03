#!/usr/bin/env python3
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAINTAINED_SOURCES = (
    "attachments.py",
    "omp_deck.py",
    "process_discovery.py",
    "projects.py",
    "server.py",
    "settings.py",
    "session_state.py",
    "static/app.css",
    "static/app.js",
    "static/composer.js",
    "static/composer.css",
    "static/index.html",
    "static/markdown.js",
    "static/sw.js",
    "static/todo-ui.js",
    "static/todo.css",
    "static/ui-helpers.js",
    "tools/check.py",
    "tools/check_commit.py",
    "tools/metrics.py",
)
SOURCE_FILES = tuple(ROOT / path for path in MAINTAINED_SOURCES)
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
                        f"{relative}:{line} {name}: complexity {complexity} exceeds "
                        f"{MAX_PYTHON_COMPLEXITY}"
                    )
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
