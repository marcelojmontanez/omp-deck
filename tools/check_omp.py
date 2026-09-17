#!/usr/bin/env python3
"""Exercise stock OMP through Deck without touching a user's profile or services."""

import argparse
import json
import os
import shutil
import signal
import tempfile
from pathlib import Path


def exercise(binary, root):
    from omp_deck._omp_version import installed_omp_version
    from omp_deck.fleet_model import FleetModel
    from omp_deck.projects import resolve_project_path
    from omp_deck.rpc_sessions import RpcSessionManager
    from omp_deck.settings import Settings

    projects = root / "projects"
    (projects / "smoke").mkdir(parents=True)
    settings = Settings(
        host="127.0.0.1",
        port=1,
        session_root=root / "sessions",
        socket_root=root / "sockets",
        state_root=root / "state",
        project_root=projects,
        project_discovery_depth=2,
        omp_bin=binary,
        allowed_hosts=("127.0.0.1",),
    )
    version = installed_omp_version(binary)
    manager = RpcSessionManager(settings, FleetModel())
    try:
        sessions = [manager.launch("smoke", resolve_project_path(projects, "smoke")) for _ in range(2)]
        for session in sessions:
            if session["transport"]["status"] != "connected" or not session["ompSessionId"]:
                raise RuntimeError("Native RPC startup did not establish a session")
    finally:
        manager.close()
    model = FleetModel()
    manager = RpcSessionManager(settings, model)
    try:
        manager.refresh_connected()
        for session in sessions:
            recovered = model.get(session["id"])
            if recovered["transport"]["status"] != "connected" or recovered["ompSessionId"] != session["ompSessionId"]:
                raise RuntimeError("Native RPC recovery did not preserve the session identity")
        manager.shutdown(sessions[0]["id"])
        if model.get(sessions[0]["id"])["transport"]["status"] == "connected":
            raise RuntimeError("Quit left its session connected")
        if manager.refresh(sessions[1]["id"])["transport"]["status"] != "connected":
            raise RuntimeError("Quitting one session disrupted another")
    finally:
        manager.close()
    print(
        json.dumps(
            {
                "ompVersion": version or "unavailable",
                "rpcStartup": "passed",
                "recovery": "passed",
                "isolatedShutdown": "passed",
            }
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--omp", default="omp", help="stock OMP executable to check")
    args = parser.parse_args()
    binary = shutil.which(args.omp)
    if binary is None:
        parser.error("OMP executable was not found")
    binary = str(Path(binary).resolve(strict=True))
    original = dict(os.environ)
    previous_signal = signal.getsignal(signal.SIGTERM)

    def terminate(_signal, _frame):
        raise SystemExit(143)

    with tempfile.TemporaryDirectory(prefix="omp-deck-native-check-") as temporary:
        root = Path(temporary).resolve()
        isolated = {key: original[key] for key in ("PATH", "SystemRoot", "SYSTEMROOT", "WINDIR") if key in original}
        for key, directory in {
            "HOME": "home",
            "USERPROFILE": "home",
            "OMP_CONFIG_DIR": "profile",
            "XDG_CONFIG_HOME": "config",
            "XDG_DATA_HOME": "data",
            "XDG_STATE_HOME": "state-home",
            "XDG_CACHE_HOME": "cache",
            "APPDATA": "appdata",
            "LOCALAPPDATA": "localappdata",
            "TMPDIR": "tmp",
            "TEMP": "tmp",
            "TMP": "tmp",
        }.items():
            (root / directory).mkdir(mode=0o700, exist_ok=True)
            isolated[key] = str(root / directory)
        isolated["OPENAI_API_KEY"] = "omp-deck-smoke-not-a-real-key"
        try:
            os.environ.clear()
            os.environ.update(isolated)
            signal.signal(signal.SIGTERM, terminate)
            exercise(binary, root)
        finally:
            signal.signal(signal.SIGTERM, previous_signal)
            os.environ.clear()
            os.environ.update(original)


if __name__ == "__main__":
    main()
