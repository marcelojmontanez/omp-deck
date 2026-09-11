from __future__ import annotations

import ctypes
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from omp_deck import lifecycle
from omp_deck._fleet_projection import platform_capabilities
from omp_deck._platform_paths import (
    default_config_path,
    default_install_root,
    default_omp_path,
    default_state_root,
)
from omp_deck.auth import AuthManager
from omp_deck.fleet_model import FleetModel
from omp_deck.projects import resolve_project_path
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
                self.assertEqual(default_install_root(), local_app_data / "OMP Deck" / "install")
                self.assertEqual(default_omp_path(), local_app_data / "omp" / "omp.exe")

    def test_task_definition_runs_as_current_interactive_user(self):
        from omp_deck._windows_lifecycle import task_xml

        definition = task_xml(
            Path(r"C:\Users\owner\AppData\Roaming\OMP Deck\config.json"),
            executable=r"C:\Tools\python.exe",
            user_sid="S-1-5-21-123",
        )
        self.assertIn("<LogonType>InteractiveToken</LogonType>", definition)
        self.assertIn("<RunLevel>LeastPrivilege</RunLevel>", definition)
        self.assertIn("<RestartOnFailure>", definition)
        self.assertIn(r"<Command>C:\Tools\python.exe</Command>", definition)
        self.assertIn("serve", definition)

    def test_windows_capabilities_include_acl_safe_attachments(self):
        self.assertTrue(platform_capabilities("rpc")["attachments"])
        self.assertTrue(platform_capabilities("collab")["attachments"])

    def test_job_wrapper_does_not_run_child_before_job_assignment(self):
        from omp_deck import _windows_job

        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "ran-before-assignment"
            assignment_entered = threading.Event()
            release_assignment = threading.Event()
            failure: list[BaseException] = []
            assign = _windows_job._assign

            def paused_assign(job, process):
                assignment_entered.set()
                if not release_assignment.wait(5):
                    raise TimeoutError("test did not release job assignment")
                return assign(job, process)

            def supervise():
                try:
                    _windows_job.main(
                        [
                            sys.executable,
                            "-c",
                            "from pathlib import Path; import sys; Path(sys.argv[1]).touch()",
                            str(marker),
                        ]
                    )
                except BaseException as error:
                    failure.append(error)

            with patch.object(_windows_job, "_assign", side_effect=paused_assign):
                supervisor = threading.Thread(target=supervise)
                supervisor.start()
                try:
                    self.assertTrue(assignment_entered.wait(5), "supervisor never attempted assignment")
                    time.sleep(0.5)
                    self.assertFalse(marker.exists(), "child executed while job assignment was paused")
                finally:
                    release_assignment.set()
                    supervisor.join(5)

            self.assertFalse(supervisor.is_alive(), "supervisor did not finish")
            self.assertEqual(failure, [])
            self.assertTrue(marker.exists(), "child was not resumed after job assignment")

    def test_job_wrapper_preserves_standard_streams(self):
        payload = b'RPC request with spaces and "quotes"\\n'
        wrapper = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "omp_deck._windows_job",
                sys.executable,
                "-c",
                "import sys; data=sys.stdin.buffer.read(); "
                "sys.stdout.buffer.write(data.upper()); "
                "sys.stderr.buffer.write(b'error-channel')",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout, stderr = wrapper.communicate(payload, timeout=10)
        self.assertEqual(wrapper.returncode, 0)
        self.assertEqual(stdout, payload.upper())
        self.assertEqual(stderr, b"error-channel")

    def test_job_wrapper_terminates_immediate_child_and_grandchild(self):
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "descendants.json"
            grandchild_code = "import time; time.sleep(60)"
            child_code = (
                "import json,subprocess,sys,time; from pathlib import Path; "
                "grandchild=subprocess.Popen([sys.executable,'-c',sys.argv[2]]); "
                "Path(sys.argv[1]).write_text(json.dumps([__import__('os').getpid(),grandchild.pid])); "
                "time.sleep(60)"
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
                    grandchild_code,
                ]
            )
            self.addCleanup(lambda: wrapper.poll() is None and wrapper.kill())
            deadline = time.monotonic() + 10
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(marker.exists(), "supervised child did not start")
            descendant_ids = json.loads(marker.read_text())
            wrapper.terminate()
            wrapper.wait(timeout=5)
            for descendant_id in descendant_ids:
                self.assertTrue(
                    _wait_for_process_exit(descendant_id, 5000),
                    f"terminating the RPC supervisor left descendant {descendant_id} running",
                )

    def test_job_wrapper_reaps_process_when_resume_fails(self):
        from omp_deck import _windows_job

        created_process_id: list[int] = []
        create_suspended = _windows_job._create_suspended

        def capture_process(command):
            process = create_suspended(command)
            created_process_id.append(process.process_id)
            return process

        with (
            patch.object(_windows_job, "_create_suspended", side_effect=capture_process),
            patch.object(_windows_job, "_resume", side_effect=OSError("forced resume failure")),
        ):
            with self.assertRaisesRegex(OSError, "forced resume failure"):
                _windows_job.main([sys.executable, "-c", "import time; time.sleep(60)"])

        self.assertEqual(len(created_process_id), 1)
        self.assertTrue(
            _wait_for_process_exit(created_process_id[0], 5000),
            "failed setup left the suspended process running",
        )

    def test_extension_adapter_uses_native_named_pipe(self):
        from omp_deck.extension_adapter import ExtensionAdapter, ExtensionAdapterError

        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is unavailable")
        session_id = "windows-pipe-session"
        connection_id = "connection-1"
        auth_token = "a" * 64
        socket_id = hashlib.sha256(session_id.encode()).hexdigest()[:24]
        endpoint = rf"\\.\pipe\{socket_id}.123.{connection_id}.pipe"
        fixture = Path(__file__).parent / "fixtures" / "fake_extension_pipe_server.mjs"
        process = subprocess.Popen(
            [node, str(fixture), endpoint, session_id, connection_id, auth_token],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        def cleanup_process():
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            process.stdout.close()
            process.stderr.close()

        self.addCleanup(cleanup_process)
        ready = threading.Event()
        output = []

        def read_ready():
            output.append(process.stdout.readline())
            ready.set()

        reader = threading.Thread(target=read_ready)
        reader.start()
        self.assertTrue(ready.wait(10), "named-pipe fixture did not start")
        self.assertEqual(output, ["READY\n"], process.stderr.read() if process.poll() is not None else "")
        denied = ExtensionAdapter(endpoint, connection_token="b" * 64)
        with self.assertRaises(ExtensionAdapterError):
            denied.start()
        denied.close()
        adapter = ExtensionAdapter(endpoint, connection_token=auth_token)
        try:
            registration = adapter.start()
            self.assertEqual(registration.session_id, session_id)
            self.assertTrue(adapter.refresh().idle)
        finally:
            adapter.close()
            process.terminate()
            process.wait(timeout=5)
            reader.join(timeout=5)

    def test_terminal_discovery_requires_private_named_pipe_metadata(self):
        from omp_deck._private_state import ensure_private_directory
        from omp_deck._terminal_discovery import discover_extension_candidates
        from omp_deck._windows_security import secure_created_path, secure_path

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "sockets"
            ensure_private_directory(root)
            session_id = "windows-discovery-session"
            connection_id = str(uuid.uuid4())
            socket_id = hashlib.sha256(session_id.encode()).hexdigest()[:24]
            name = f"{socket_id}.{os.getpid()}.{connection_id}.pipe"
            endpoint = rf"\\.\pipe\{name}"
            metadata = root / f"{name}.json"
            metadata.write_text(
                json.dumps(
                    {
                        "protocol": "omp-deck-fallback-v1",
                        "version": 1,
                        "type": "registration",
                        "sessionId": session_id,
                        "connectionId": connection_id,
                        "endpoint": endpoint,
                        "pid": os.getpid(),
                        "authToken": "a" * 64,
                    }
                ),
                encoding="utf-8",
            )
            secure_created_path(metadata)
            candidates = discover_extension_candidates(root)
            self.assertEqual(set(candidates), {session_id})
            discovered_endpoint, discovered_metadata = candidates[session_id]
            self.assertEqual(str(discovered_endpoint), endpoint)
            self.assertEqual(discovered_metadata["connectionId"], connection_id)
            subprocess.run(
                ["icacls.exe", str(root), "/grant", "*S-1-1-0:(OI)(CI)M", "/Q"],
                check=True,
                capture_output=True,
            )
            try:
                self.assertEqual(discover_extension_candidates(root), {})
            finally:
                secure_path(root)


@unittest.skipUnless(
    _WINDOWS and os.environ.get("OMP_DECK_WINDOWS_TASK_SMOKE") == "1",
    "set OMP_DECK_WINDOWS_TASK_SMOKE=1 for Task Scheduler integration",
)
class WindowsTaskIntegrationTests(unittest.TestCase):
    def test_scheduled_task_starts_healthy_server_and_is_removed(self):
        from omp_deck import _windows_lifecycle

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            environment = {
                "APPDATA": str(root / "roaming"),
                "LOCALAPPDATA": str(root / "local"),
            }
            task_name = f"OMP Deck Test {uuid.uuid4()}"
            task_path = rf"\{task_name}"
            state_command = f"(Get-ScheduledTask -TaskName '{task_name}' -ErrorAction Stop).State.ToString()"

            def cleanup():
                subprocess.run(
                    ["schtasks.exe", "/End", "/TN", task_path],
                    check=False,
                    capture_output=True,
                )
                subprocess.run(
                    ["schtasks.exe", "/Delete", "/TN", task_path, "/F"],
                    check=False,
                    capture_output=True,
                )

            self.addCleanup(cleanup)
            with (
                patch.dict(os.environ, environment),
                patch.multiple(
                    _windows_lifecycle,
                    _TASK_NAME=task_name,
                    _TASK_PATH=task_path,
                    _QUERY=("schtasks.exe", "/Query", "/TN", task_path),
                    _STATE_COMMAND=state_command,
                ),
            ):
                config = root / "roaming" / "OMP Deck" / "config.json"
                state_root = root / "local" / "OMP Deck" / "state"
                project_root = root / "projects"
                project_root.mkdir()
                settings = Settings(
                    host="127.0.0.1",
                    port=port,
                    session_root=root / "sessions",
                    socket_root=state_root / "sockets",
                    state_root=state_root,
                    project_root=project_root,
                    project_discovery_depth=2,
                    omp_bin=sys.executable,
                    allowed_hosts=("127.0.0.1", "localhost"),
                )
                _windows_lifecycle._write_private(
                    config,
                    json.dumps(
                        {
                            "host": settings.host,
                            "port": port,
                            "state_root": str(state_root),
                            "project_root": str(project_root),
                            "session_root": str(settings.session_root),
                            "socket_root": str(settings.socket_root),
                            "omp_bin": sys.executable,
                        }
                    ).encode(),
                )
                AuthManager(state_root)
                lifecycle.install_service(settings, config)
                try:
                    self.assertTrue(lifecycle.check_health(settings, timeout=15)["ok"])
                    self.assertEqual(lifecycle.service_status(), 0)
                finally:
                    lifecycle.remove_service()
                query = subprocess.run(
                    ["schtasks.exe", "/Query", "/TN", task_path],
                    check=False,
                    capture_output=True,
                )
                self.assertNotEqual(query.returncode, 0)


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
                session = manager.launch("smoke", resolve_project_path(project_root, "smoke"))
                self.assertEqual(session["transport"]["status"], "connected")
                self.assertEqual(session["transport"]["kind"], "rpc")
                self.assertTrue(session["ompSessionId"])
            finally:
                manager.close()
