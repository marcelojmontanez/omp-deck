from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

from . import check_receipt
from .node_artifacts import inspect_wheel
from .storage import ReleaseError

# Until a reviewed migration is implemented, changed persistence/wire contracts
# are not presumed safe merely because HTTP health succeeds.
CONTRACT_FILES = (
    "omp_deck/_auth_state.py",
    "omp_deck/request_ledger.py",
    "omp_deck/registry_store.py",
    "omp_deck/coordinator_store.py",
    "omp_deck/fleet_protocol.py",
    "omp_deck/_extension_protocol.py",
)


def artifact(path: Path) -> dict:
    digest, version, plugin = inspect_wheel(path)
    match = re.fullmatch(r".+\+g([0-9a-f]{40})", version)
    if match is None:
        raise ReleaseError("release wheel lacks immutable commit identity")
    return {
        "wheelPath": str(path),
        "wheelSha256": digest,
        "wheelVersion": version,
        "revision": match[1],
        "pluginSha256": plugin,
    }


def compatible(candidate: Path, rollback: Path) -> None:
    with zipfile.ZipFile(candidate) as new, zipfile.ZipFile(rollback) as old:
        for name in CONTRACT_FILES:
            try:
                identical = new.read(name) == old.read(name)
            except KeyError:
                identical = False
            if not identical:
                raise ReleaseError(
                    "persistence/protocol change requires a reviewed compatibility migration before rolling release"
                )


def run_check(repository: Path, argv: list[str], *, release: bool = False) -> None:
    environment = {
        **os.environ,
        "PYTHONPATH": str(repository / "src"),
        # The hook must use this interpreter, not a stale shell activation.
        "VIRTUAL_ENV": sys.prefix if sys.prefix != sys.base_prefix else "",
        **({"OMP_DECK_PACKAGING_TESTS": "1"} if release else {}),
    }
    result = subprocess.run(argv, cwd=repository, env=environment, timeout=1200, check=False)
    if result.returncode:
        raise ReleaseError("release validation failed")


def validate(repository: Path) -> None:
    before = check_receipt.fingerprint(repository)
    if before is None:
        raise ReleaseError("release requires a fully staged or clean tracked checkout with no untracked inputs")
    if not check_receipt.valid(repository):
        run_check(repository, [sys.executable, "tools/check.py", "--release"])
    if not check_receipt.valid(repository, release=True):
        run_check(repository, [sys.executable, "-m", "unittest", "tests.test_packaging_identity"], release=True)
        for argv in (
            ["npm", "run", "test:browser"],
            ["npm", "--prefix", "src/omp_deck/plugin", "run", "check"],
        ):
            run_check(repository, argv)
        check_receipt.record(repository, before, release=True)
    if before != check_receipt.fingerprint(repository):
        raise ReleaseError("source or verification environment changed during validation")


def bundle(repository: Path, destination: Path) -> None:
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "__main__.py",
            "import sys\nfrom release_lib.transport import worker_entry\nsys.exit(worker_entry(sys.argv[1:]))\n",
        )
        for source, prefix in (
            (repository / "tools/release_lib", "release_lib"),
            (repository / "src/omp_deck", "omp_deck"),
        ):
            for path in sorted(source.glob("*.py")):
                if path.is_symlink():
                    raise ReleaseError("worker source cannot be a symlink")
                archive.write(path, f"{prefix}/{path.name}")
    destination.chmod(0o600)


def prepare_build(repository: Path, release_root: Path, *, message: str | None = None) -> dict:
    if check_receipt.git(repository, "branch", "--show-current").strip() != b"main":
        raise ReleaseError("release requires main; no branch is created automatically")
    staged = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=repository).returncode
    if staged not in {0, 1}:
        raise ReleaseError("cannot inspect staged source")
    if staged and not message:
        raise ReleaseError("staged release changes require --message for the existing Conventional Commit hook")
    if message:
        run_check(repository, ["git", "commit", "-m", message])
    validate(repository)
    checked_source = check_receipt.fingerprint(repository)
    revision = check_receipt.git(repository, "rev-parse", "HEAD").decode().strip()
    output = release_root / "artifacts"
    output.mkdir(mode=0o700)
    run_check(repository, [sys.executable, "-m", "build", "--wheel", "--outdir", str(output)])
    wheels = list(output.glob("*.whl"))
    if len(wheels) != 1:
        raise ReleaseError("expected one immutable release wheel")
    result = artifact(wheels[0])
    if result["revision"] != revision:
        raise ReleaseError("built revision differs from validated source")
    bundle(repository, release_root / "worker.pyz")
    if check_receipt.fingerprint(repository) != checked_source:
        raise ReleaseError("checked source changed while assembling release artifacts")
    result["workerSha256"] = hashlib.sha256((release_root / "worker.pyz").read_bytes()).hexdigest()
    return result
