from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path

from omp_deck._private_state import PrivateStateFile

from .storage import MAX_JSON, ReleaseError, absolute, directory


class Transport:
    def __init__(self, target: dict, transaction: str, local_root: Path, repository: Path):
        self.target = target
        self.transaction = transaction
        self.local_root = directory(local_root)
        self.repository = repository
        self.root = str(absolute(target["work_root"]) / transaction)
        self.python = target["profile"]["base_python"]
        self.worker_path = f"{self.root}/worker.pyz"

    def command(self, arguments: list[str], *, payload: bytes | None = None, timeout: int = 600) -> bytes:
        argv = arguments
        if self.target.get("ssh"):
            argv = [
                "ssh",
                "-T",
                "-oBatchMode=yes",
                "-oConnectTimeout=10",
                "-oServerAliveInterval=15",
                "-oServerAliveCountMax=2",
                "--",
                self.target["ssh"],
                shlex.join(arguments),
            ]
        try:
            result = subprocess.run(argv, input=payload, capture_output=True, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ReleaseError(f"{self.target['id']}: command unavailable or timed out") from error
        if result.returncode:
            raise ReleaseError(f"{self.target['id']}: operation failed (exit {result.returncode})")
        return result.stdout

    def transfer(self, header: dict, payload: bytes = b"") -> bytes:
        source = (self.repository / "tools/release_lib/bootstrap.py").read_text()
        return self.command(
            [self.python, "-c", source], payload=json.dumps(header).encode() + b"\n" + payload, timeout=120
        )

    def upload(self, source: Path, name: str) -> None:
        if source.is_symlink() or not source.is_file() or source.stat().st_size > 128 * 1024 * 1024:
            raise ReleaseError("unsafe or oversized upload source")
        payload = source.read_bytes()
        self.transfer({"operation": "upload", "root": self.root, "name": name, "size": len(payload)}, payload)

    def download(self, remote: str, destination: Path) -> None:
        payload = self.transfer({"operation": "download", "path": str(absolute(remote))})
        if len(payload) > 128 * 1024 * 1024:
            raise ReleaseError("oversized download")
        storage = PrivateStateFile(destination, repair_root=False)
        with storage.locked():
            storage.replace(payload)

    def stage(self, bundle: Path, wheel: Path, rollback: Path, request: dict) -> None:
        self.upload(bundle, "worker.pyz")
        self.upload(wheel, f"candidate/{wheel.name}")
        self.upload(rollback, f"rollback/{rollback.name}")
        request = {**request, "_worker_argv": [self.python, self.worker_path]}
        payload = json.dumps(request).encode()
        self.transfer({"operation": "upload", "root": self.root, "name": "request.json", "size": len(payload)}, payload)

    def action(self, action: str) -> dict:
        raw = self.command([self.python, self.worker_path, "worker", action, f"{self.root}/request.json"])
        if len(raw) > MAX_JSON:
            raise ReleaseError("oversized worker response")
        try:
            result = json.loads(raw)
        except (UnicodeError, ValueError) as error:
            raise ReleaseError(f"{self.target['id']}: invalid worker response") from error
        if not isinstance(result, dict):
            raise ReleaseError(f"{self.target['id']}: {action} returned invalid state")
        if result.get("error"):
            raise ReleaseError(f"{self.target['id']}: {action} failed ({result['error']})")
        return result

    def verify(self) -> dict:
        result = self.action("verify_prepare")
        expected = f"{self.root}/browser-input.json"
        if result.get("browserInputPath") != expected:
            raise ReleaseError("verification input escaped transaction")
        source, output = self.local_root / "browser-input.json", self.local_root / "browser-result.json"
        try:
            self.download(expected, source)
            run_browser(self.repository, "--input", str(source), "--output", str(output))
            self.upload(output, "browser-result.json")
            return self.action("verify")
        finally:
            source.unlink(missing_ok=True)
            output.unlink(missing_ok=True)


def run_browser(repository: Path, *arguments: str) -> None:
    try:
        result = subprocess.run(
            ["node", str(repository / "tools/release_browser.mjs"), *arguments],
            cwd=repository,
            capture_output=True,
            timeout=90,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ReleaseError("release browser unavailable or timed out") from error
    if result.returncode:
        raise ReleaseError("read-only release browser verification failed")


def worker_entry(arguments: list[str]) -> int:
    import contextlib

    from . import worker
    from .storage import load

    try:
        if arguments[0] == "watchdog" and len(arguments) == 2:
            worker.watchdog(load(Path(arguments[1])))
            return 0
        if arguments[0] != "worker" or len(arguments) != 3:
            raise ReleaseError("invalid worker invocation")
        request = load(Path(arguments[2]))
        with contextlib.redirect_stdout(sys.stderr):
            result = worker.execute(arguments[1], request)
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as error:
        # Remote stderr may otherwise disclose credential-bearing CLI output.
        print(json.dumps({"error": type(error).__name__}), file=sys.stdout)
        return 0 if arguments and arguments[0] == "worker" else 1
