#!/usr/bin/env python3
"""Prepare, deploy, inspect, or resume a guarded fleet release."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from release_lib.orchestrator import open_release  # noqa: E402
from release_lib.storage import ReleaseError, default_config  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=default_config(), help="owner-only release configuration")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "run"):
        command = commands.add_parser(
            name, help="validate/build/stage" if name == "prepare" else "prepare and deploy the fleet"
        )
        command.add_argument("--message", help="commit already-staged changes through the repository hook")
    for name in ("deploy", "status", "resume"):
        commands.add_parser(name).add_argument("release")
    args = parser.parse_args(argv)
    try:
        if os.name == "nt":
            raise ReleaseError("release automation must run from a macOS or Linux operator host")
        release = open_release(args.config, ROOT, getattr(args, "release", None))
        if args.command in {"prepare", "run"}:
            result = release.prepare(args.message)
            if args.command == "run":
                result = release.deploy()
        elif args.command == "status":
            result = release.status()
        else:
            result = release.deploy(resume=args.command == "resume")
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0
    except (ReleaseError, OSError, ValueError) as error:
        reason = str(error) if isinstance(error, ReleaseError) else type(error).__name__
        print(f"release stopped: {reason}", file=sys.stderr)
        if "release" in locals():
            print(f"release ID: {release.transaction}", file=sys.stderr)
            if release.journal:
                print(json.dumps(release.summary(), sort_keys=True, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
