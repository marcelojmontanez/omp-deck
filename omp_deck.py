#!/usr/bin/env python3
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from settings import CONFIG_PATH, SETTINGS


ROOT = Path(__file__).resolve().parent
RUNTIME_FILES = (
    "attachments.py",
    "process_discovery.py",
    "projects.py",
    "read_cursors.py",
    "server.py",
    "session_projection.py",
    "session_state.py",
    "settings.py",
    "omp_deck.py",
)


def user_paths():
    home = Path.home()
    data_home = Path(os.environ.get("XDG_DATA_HOME", home / ".local/share"))
    bin_home = Path(os.environ.get("OMP_DECK_BIN_DIR", home / ".local/bin"))
    unit_home = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config")) / "systemd/user"
    return data_home / "omp-deck", bin_home / "omp-deck", unit_home / "omp-deck.service"

def extension_path():
    agent_root = Path(os.environ.get("PI_CODING_AGENT_DIR", Path.home() / ".omp/agent"))
    return agent_root / "extensions/omp-deck.ts"


def run(command, check=True):
    return subprocess.run(command, check=check, text=True)


def write_atomic(path, content, mode=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.new")
    temporary.write_text(content, encoding="utf-8")
    if mode is not None:
        temporary.chmod(mode)
    os.replace(temporary, path)

def enable_tailscale():
    if shutil.which("tailscale") is None:
        return None
    status = subprocess.run(
        ["tailscale", "serve", "status", "--json"],
        capture_output=True,
        text=True,
    )
    try:
        existing = json.loads(status.stdout) if status.returncode == 0 else {}
    except json.JSONDecodeError:
        existing = {}
    if existing.get("Web") or existing.get("TCP"):
        print("Existing Tailscale Serve configuration left unchanged.")
        return None
    enabled = subprocess.run(
        ["tailscale", "serve", "--bg", "--yes", str(SETTINGS.port)],
        capture_output=True,
        text=True,
    )
    if enabled.returncode:
        print("Tailscale detected, but automatic phone access could not be enabled.")
        print(f"Run manually: tailscale serve --bg {SETTINGS.port}")
        return None
    tailscale = subprocess.run(
        ["tailscale", "status", "--json"],
        capture_output=True,
        text=True,
    )
    try:
        name = json.loads(tailscale.stdout)["Self"]["DNSName"].rstrip(".")
    except (KeyError, json.JSONDecodeError):
        return None
    return f"https://{name}"


def install():
    if sys.platform != "linux" or shutil.which("systemctl") is None:
        raise RuntimeError("automatic installation currently requires Linux with systemd")
    if shutil.which("omp") is None and not Path(SETTINGS.omp_bin).is_file():
        raise RuntimeError("OMP is not installed or not on PATH")

    install_root, command_path, unit_path = user_paths()
    install_root.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".omp-deck-", dir=install_root.parent))
    try:
        for name in RUNTIME_FILES:
            shutil.copy2(ROOT / name, staging / name)
        shutil.copytree(ROOT / "static", staging / "static")
        if install_root.exists():
            shutil.rmtree(install_root)
        os.replace(staging, install_root)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    if not CONFIG_PATH.exists():
        write_atomic(CONFIG_PATH, "{}\n", 0o600)
    write_atomic(
        extension_path(),
        (ROOT / "omp-extension/omp-deck.ts").read_text(encoding="utf-8"),
        0o644,
    )
    extension_path().with_name("mobile-bridge.ts").unlink(missing_ok=True)
    write_atomic(
        command_path,
        f"#!/bin/sh\nexec {sys.executable} {install_root / 'omp_deck.py'} \"$@\"\n",
        0o755,
    )
    write_atomic(
        unit_path,
        "\n".join((
            "[Unit]",
            "Description=OMP Deck",
            "After=default.target",
            "",
            "[Service]",
            "Type=simple",
            f"WorkingDirectory={install_root}",
            f"ExecStart={sys.executable} {install_root / 'server.py'}",
            "Restart=on-failure",
            "RestartSec=2",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        )),
    )
    run(["systemctl", "--user", "daemon-reload"])
    run(["systemctl", "--user", "enable", "omp-deck.service"])
    run(["systemctl", "--user", "restart", "omp-deck.service"])
    phone_url = enable_tailscale()
    print(f"OMP Deck is running at {phone_url or f'http://{SETTINGS.host}:{SETTINGS.port}'}")
    if phone_url is None and shutil.which("tailscale") is None:
        print("Install Tailscale on this computer and phone for secure remote access.")


def doctor():
    checks = (
        ("OMP executable", Path(SETTINGS.omp_bin).is_file() or shutil.which(SETTINGS.omp_bin) is not None),
        ("OMP sessions", SETTINGS.session_root.is_dir()),
        ("OMP Deck connection", SETTINGS.socket_root.is_dir()),
        ("OMP Deck extension", extension_path().is_file()),
        ("Project root", SETTINGS.project_root.is_dir()),
        ("Local-only listener", SETTINGS.host in {"127.0.0.1", "::1", "localhost"}),
    )
    for label, healthy in checks:
        print(f"{'OK' if healthy else 'FAIL':4}  {label}")
    service = run(["systemctl", "--user", "is-active", "--quiet", "omp-deck.service"], check=False)
    print(f"{'OK' if service.returncode == 0 else 'FAIL':4}  Background service")
    return 0 if all(healthy for _, healthy in checks) and service.returncode == 0 else 1


def uninstall(purge=False):
    install_root, command_path, unit_path = user_paths()
    run(["systemctl", "--user", "disable", "--now", "omp-deck.service"], check=False)
    unit_path.unlink(missing_ok=True)
    run(["systemctl", "--user", "daemon-reload"], check=False)
    command_path.unlink(missing_ok=True)
    shutil.rmtree(install_root, ignore_errors=True)
    extension_path().unlink(missing_ok=True)
    if purge:
        CONFIG_PATH.unlink(missing_ok=True)
        shutil.rmtree(SETTINGS.state_root, ignore_errors=True)
    print("OMP Deck removed" + (" with its local data" if purge else "; config and state preserved"))


def serve():
    from server import Handler, HOST, PORT, ThreadingHTTPServer

    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"OMP Deck ready at http://{HOST}:{PORT}", flush=True)
    httpd.serve_forever()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="omp-deck")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("install", help="install and start OMP Deck")
    commands.add_parser("doctor", help="check the local installation")
    remove = commands.add_parser("uninstall", help="remove OMP Deck")
    remove.add_argument("--purge", action="store_true", help="also delete configuration and state")
    commands.add_parser("serve", help="run OMP Deck in the foreground")
    arguments = parser.parse_args(argv)
    if arguments.command == "install":
        install()
    elif arguments.command == "doctor":
        return doctor()
    elif arguments.command == "uninstall":
        uninstall(arguments.purge)
    else:
        serve()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.CalledProcessError, ValueError) as error:
        print(f"omp-deck: {error}", file=sys.stderr)
        raise SystemExit(1)
