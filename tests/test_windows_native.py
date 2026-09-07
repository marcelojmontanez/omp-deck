from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from omp_deck import cli
from omp_deck._fleet_projection import platform_capabilities
from omp_deck._platform_paths import (
    default_config_path,
    default_install_root,
    default_omp_path,
    default_state_root,
)
from omp_deck.fleet_model import FleetModel
from omp_deck.rpc_sessions import RpcSessionManager
from omp_deck.settings import Settings

_WINDOWS = sys.platform == "win32"
_SYNCHRONIZE = 0x00100000
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 258


def _wait_for_process_exit(process_id: int, timeout_ms: int) -> bool:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_ulong)
    kernel32.WaitForSingleObject.restype = ctypes.c_ulong
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    handle = kernel32.OpenProcess(_SYNCHRONIZE, False, process_id)
    if not handle:
        return True
    try:
        result = kernel32.WaitForSingleObject(handle, timeout_ms)
        if result not in {_WAIT_OBJECT_0, _WAIT_TIMEOUT}:
            raise ctypes.WinError(ctypes.get_last_error())
        return result == _WAIT_OBJECT_0
    finally:
        kernel32.CloseHandle(handle)


@unittest.skipUnless(_WINDOWS, "native Windows contract")
class WindowsPlatformTests(unittest.TestCase):
    def test_defaults_use_per_user_windows_directories(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app_data = root / "roaming"
            local_app_data = root / "local"
            with patch.dict(
                os.environ,
                {"APPDATA": str(app_data), "LOCALAPPDATA": str(local_app_data)},
            ):
                self.assertEqual(default_config_path(), app_data / "OMP Deck" / "config.json")
                self.assertEqual(default_state_root(), local_app_data / "OMP Deck" / "state")
                self.assertEqual(default_install_root(), local_app_data / "OMP Deck")
                self.assertEqual(default_omp_path(), local_app_data / "omp" / "omp.exe")

    def test_cli_refuses_unsupported_service_management(self):
        with patch.object(cli, "service_status") as service_status:
            with self.assertRaisesRegex(RuntimeError, "unavailable on native Windows"):
                cli.main(["service", "status"])
        service_status.assert_not_called()

    def test_windows_capabilities_disable_blob_attachments(self):
        self.assertFalse(platform_capabilities("rpc")["attachments"])
        self.assertFalse(platform_capabilities("collab")["attachments"])

    def test_job_wrapper_terminates_descendant_processes(self):
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "descendant.pid"
            child_code = (
                "import subprocess,sys,time; from pathlib import Path; "
                "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
                "Path(sys.argv[1]).write_text(str(child.pid)); time.sleep(60)"
            )
            wrapper = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "omp_deck._windows_job",
                    sys.executable,
                    "-c",
                    child_code,
                    str(marker),
                ]
            )
            self.addCleanup(lambda: wrapper.poll() is None and wrapper.kill())
            deadline = time.monotonic() + 10
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(marker.exists(), "supervised child did not start")
            descendant_id = int(marker.read_text())
            wrapper.terminate()
            wrapper.wait(timeout=5)
            self.assertTrue(
                _wait_for_process_exit(descendant_id, 5000),
                "terminating the RPC supervisor left a descendant running",
            )


@unittest.skipUnless(
    _WINDOWS and bool(os.environ.get("OMP_DECK_WINDOWS_OMP_SMOKE_BIN")),
    "set OMP_DECK_WINDOWS_OMP_SMOKE_BIN for native RPC integration",
)
class WindowsRpcIntegrationTests(unittest.TestCase):
    def test_managed_rpc_starts_and_stops_official_omp(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project_root = root / "projects"
            project = project_root / "smoke"
            project.mkdir(parents=True)
            settings = Settings(
                host="127.0.0.1",
                port=8787,
                session_root=root / "sessions",
                socket_root=root / "sockets",
                state_root=root / "state",
                project_root=project_root,
                project_discovery_depth=2,
                omp_bin=os.environ["OMP_DECK_WINDOWS_OMP_SMOKE_BIN"],
                allowed_hosts=("127.0.0.1", "localhost"),
            )
            manager = RpcSessionManager(settings, FleetModel())
            try:
                session = manager.launch("smoke", project)
                self.assertEqual(session["transport"]["status"], "connected")
                self.assertEqual(session["transport"]["kind"], "rpc")
                self.assertTrue(session["ompSessionId"])
            finally:
                manager.close()
