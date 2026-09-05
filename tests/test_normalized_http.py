import base64
import contextlib
import http.client
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from omp_deck import server
from omp_deck._terminal_commands import TerminalCommandConflict
from omp_deck.fleet_model import FleetModel
from omp_deck.rpc_sessions import SessionCommandConflictError


class _Manager:
    def __init__(self, conflict_type):
        self.conflict_type = conflict_type
        self.calls = []
        self.removals = []
        self.imports = []
        self.import_error = None
        self._requests = {}

    def request_refresh_connected(self):
        return None

    def _dispatch(self, session_id, request_id, action, payload):
        signature = (session_id, action, json.dumps(payload, sort_keys=True))
        previous = self._requests.get(request_id)
        if previous is not None and previous != signature:
            raise self.conflict_type("requestId was already used for another command")
        self._requests[request_id] = signature
        self.calls.append((session_id, request_id, action, payload))
        return {"requestId": request_id, "status": "accepted"}

    def dispatch(self, session_id, request_id, action, payload):
        return self._dispatch(session_id, request_id, action, payload)

    def command(self, session_id, request_id, action, payload):
        return self._dispatch(session_id, request_id, action, payload)

    def add_collab_request(self, request_id, link, title=None):
        self.imports.append((request_id, link, title))
        if self.import_error is not None:
            raise RuntimeError(self.import_error)
        return {
            "requestId": request_id,
            "status": "accepted",
            "session": {"id": "imported", "title": title or "Imported"},
        }

    def remove(self, session_id):
        self.removals.append(session_id)
        return session_id == "terminal-1"


class NormalizedHttpTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.project_root = root / "projects-private-root"
        self.project_root.mkdir()
        settings = SimpleNamespace(
            allowed_hosts=("127.0.0.1",),
            state_root=root / "state",
        )

        self.model = FleetModel()
        self.model.load_record(self.record("rpc-1", "rpc", "/secret/work/rpc"))
        self.model.load_record(self.record("terminal-1", "collab", "/secret/work/terminal"))
        self.rpc = _Manager(SessionCommandConflictError)
        self.terminal = _Manager(TerminalCommandConflict)
        for target, value in (
            ("SETTINGS", settings),
            ("DEV_ROOT", self.project_root),
            ("_fleet_model", self.model),
            ("_rpc_manager", self.rpc),
            ("_terminal_manager", self.terminal),
            ("_auth_manager", None),
        ):
            started = patch.object(server, target, value)
            started.start()
            self.addCleanup(started.stop)

        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.host = f"127.0.0.1:{self.httpd.server_port}"
        self.origin = f"http://{self.host}"
        self.cookie, self.csrf = self.pair()

    @staticmethod
    def record(session_id, adapter_kind, project_path):
        return {
            "id": session_id,
            "title": f"Title {session_id}",
            "project": {"name": session_id, "path": project_path},
            "adapterKind": adapter_kind,
            "ompSessionId": f"omp-{session_id}",
            "generation": 1,
            "updatedAt": "2026-09-03T12:00:00+00:00",
        }

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port)
        payload = None if body is None else json.dumps(body, separators=(",", ":"))
        request_headers = {"Host": self.host, **(headers or {})}
        if payload is not None:
            request_headers["Content-Type"] = "application/json"
        connection.request(method, path, body=payload, headers=request_headers)
        response = connection.getresponse()
        raw = response.read()
        result = response.status, dict(response.headers), json.loads(raw)
        connection.close()
        return result

    def pair(self):
        secret = server.auth_manager().begin_pairing()
        status, headers, payload = self.request(
            "POST",
            "/api/auth/pair",
            {"token": secret.token},
            {"Origin": self.origin, "Sec-Fetch-Site": "same-origin"},
        )
        self.assertEqual(status, 200)
        return headers["Set-Cookie"].split(";", 1)[0], payload["csrfToken"]

    def authenticated_headers(self):
        return {
            "Cookie": self.cookie,
            "Origin": self.origin,
            "Sec-Fetch-Site": "same-origin",
            "X-OMP-Deck-CSRF": self.csrf,
        }

    def post(self, path, body, headers=None):
        return self.request("POST", path, body, self.authenticated_headers() if headers is None else headers)

    def enable(self, session_id, capability):
        with self.model._lock:
            self.model._sessions[session_id]["capabilities"][capability] = True

    def test_get_sessions_merges_normalized_transports_and_project_root_is_only_a_label(self):
        status, _, payload = self.request("GET", "/api/sessions", headers={"Cookie": self.cookie})
        self.assertEqual(status, 200)
        sessions = {item["id"]: item for item in payload["sessions"]}
        self.assertEqual(set(sessions), {"rpc-1", "terminal-1"})
        self.assertEqual(sessions["rpc-1"]["transport"]["kind"], "rpc")
        self.assertEqual(sessions["terminal-1"]["transport"]["kind"], "collab")
        self.assertEqual(sessions["rpc-1"]["project"], {"name": "rpc-1", "path": "/secret/work/rpc"})
        self.assertIn("capabilities", sessions["terminal-1"])

        status, _, projects = self.request("GET", "/api/projects", headers={"Cookie": self.cookie})
        self.assertEqual(status, 200)
        self.assertEqual(projects["rootLabel"], "projects-private-root/")
        self.assertNotIn(str(self.project_root), json.dumps(projects))

    def test_listed_nested_project_can_be_launched_through_encoded_route(self):
        nested = self.project_root / "team/service"
        nested.mkdir(parents=True)
        accepted = {
            "requestId": "nested-launch",
            "status": "accepted",
            "session": {"id": "nested-session"},
            "created": False,
        }

        with patch.object(server, "launch_rpc_project", return_value=accepted) as launch:
            status, _, payload = self.post(
                "/api/projects/team%2Fservice/sessions",
                {"requestId": "nested-launch"},
            )

        self.assertEqual((status, payload), (201, accepted))
        launch.assert_called_once_with("team/service", "nested-launch", False, None)

    def test_collab_import_failure_never_returns_or_logs_the_import_link(self):
        link = "omp://collab/import?secret=never-expose-this"
        self.terminal.import_error = f"connection rejected for {link}"
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status, _, payload = self.post(
                "/api/terminal-sessions/collab",
                {
                    "requestId": "private-import",
                    "link": link,
                    "title": "Remote",
                },
            )
        self.assertEqual(status, 502)
        self.assertEqual(payload, {"error": "Collaboration connection unavailable"})
        self.assertNotIn(link, json.dumps(payload))
        self.assertNotIn(link, output.getvalue())

    def test_commands_dispatch_to_rpc_or_terminal_manager(self):
        status, _, _ = self.post("/api/sessions/rpc-1/messages", {"requestId": "rpc-msg", "message": " hello "})
        self.assertEqual(status, 202)
        status, _, _ = self.post("/api/sessions/terminal-1/interrupt", {"requestId": "terminal-stop"})
        self.assertEqual(status, 202)
        self.assertEqual(
            self.rpc.calls,
            [
                (
                    "rpc-1",
                    "rpc-msg",
                    "sendMessage",
                    {"message": "hello", "streamingBehavior": None, "images": None},
                )
            ],
        )
        self.assertEqual(self.terminal.calls, [("terminal-1", "terminal-stop", "interrupt", {})])

    def test_capability_rejection_occurs_before_any_dispatch(self):
        status, _, payload = self.post(
            "/api/sessions/terminal-1/follow-ups",
            {"requestId": "unsupported", "message": "later"},
        )
        self.assertEqual(status, 409)
        self.assertEqual(payload, {"requestId": "unsupported", "status": "failed", "errorCode": "unsupported"})
        self.assertEqual(self.rpc.calls, [])
        self.assertEqual(self.terminal.calls, [])

    def test_conflicting_request_id_reuse_is_a_409(self):
        first = {"requestId": "same-id", "message": "first"}
        second = {"requestId": "same-id", "message": "different"}
        self.assertEqual(self.post("/api/sessions/rpc-1/messages", first)[0], 202)
        status, _, payload = self.post("/api/sessions/rpc-1/messages", second)
        self.assertEqual(status, 409)
        self.assertEqual(payload["errorCode"], "request_conflict")
        self.assertNotIn("different", payload["error"])

    def test_message_and_follow_up_decode_images_to_exact_manager_wire_shape(self):
        self.enable("rpc-1", "attachments")
        raw = b"\x89PNG\r\n\x1a\nimage-bytes\xff"
        encoded = base64.b64encode(raw).decode("ascii")
        attachment = {
            "name": "capture.png",
            "type": "image/png",
            "data": f"data:image/png;base64,{encoded}",
        }
        status, _, _ = self.post(
            "/api/sessions/rpc-1/messages",
            {
                "requestId": "image-message",
                "message": " inspect ",
                "streamingBehavior": "steer",
                "attachments": [attachment],
            },
        )
        self.assertEqual(status, 202)
        status, _, _ = self.post(
            "/api/sessions/rpc-1/follow-ups",
            {"requestId": "image-follow-up", "message": "next", "attachments": [attachment]},
        )
        self.assertEqual(status, 202)
        image = {"type": "image", "data": encoded, "mimeType": "image/png"}
        self.assertEqual(
            self.rpc.calls,
            [
                (
                    "rpc-1",
                    "image-message",
                    "sendMessage",
                    {"message": "inspect", "streamingBehavior": "steer", "images": [image]},
                ),
                ("rpc-1", "image-follow-up", "followUp", {"message": "next", "images": [image]}),
            ],
        )

    def test_image_validation_rejects_count_type_base64_and_decoded_size(self):
        self.enable("rpc-1", "attachments")
        png = base64.b64encode(b"\x89PNG\r\n\x1a\nvalid").decode("ascii")
        valid = {
            "name": "x.png",
            "type": "image/png",
            "data": f"data:image/png;base64,{png}",
        }
        cases = (
            ([valid] * 5, "invalid attachments"),
            ([{"name": "x", "type": "text/plain", "data": "data:text/plain;base64,AA=="}], "invalid image attachment"),
            ([{"name": "x", "type": "image/png", "data": "data:image/png;base64,%%%"}], "invalid image attachment"),
            (
                [{"name": "fake.png", "type": "image/png", "data": "data:image/png;base64,AA=="}],
                "invalid image attachment",
            ),
            (
                [
                    {
                        "name": "x.png",
                        "type": "image/png",
                        "data": "data:image/png;base64," + base64.b64encode(b"\xff\xd8\xffjpeg").decode("ascii"),
                    }
                ],
                "invalid image attachment",
            ),
            (
                [
                    {
                        "name": "x",
                        "type": "image/png",
                        "data": "data:image/png;base64,"
                        + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * (700 * 1024)).decode("ascii"),
                    }
                ],
                "image attachments exceed 700 KiB",
            ),
        )
        for index, (attachments, error) in enumerate(cases):
            with self.subTest(error=error):
                status, _, payload = self.post(
                    "/api/sessions/rpc-1/messages",
                    {
                        "requestId": f"bad-image-{index}",
                        "message": "x",
                        "attachments": attachments,
                    },
                )
                self.assertEqual(status, 400)
                self.assertEqual(payload, {"error": error})
        self.assertEqual(self.rpc.calls, [])

    def test_ui_respond_and_cancel_preserve_allowed_values(self):
        status, _, _ = self.post(
            "/api/sessions/rpc-1/ui-requests/prompt-1/respond",
            {"requestId": "ui-text", "value": "typed value"},
        )
        self.assertEqual(status, 202)
        status, _, _ = self.post(
            "/api/sessions/rpc-1/ui-requests/confirm-1/respond",
            {"requestId": "ui-bool", "value": False},
        )
        self.assertEqual(status, 202)
        self.enable("terminal-1", "answerUiRequest")
        status, _, _ = self.post(
            "/api/sessions/terminal-1/ui-requests/7/respond",
            {"requestId": "ui-multi", "value": ["alpha", "beta"]},
        )
        self.assertEqual(status, 202)
        status, _, _ = self.post(
            "/api/sessions/rpc-1/ui-requests/prompt-2/cancel",
            {"requestId": "ui-cancel"},
        )
        self.assertEqual(status, 202)
        self.assertEqual(
            self.rpc.calls,
            [
                ("rpc-1", "ui-text", "respondToUi", {"requestId": "prompt-1", "value": "typed value"}),
                ("rpc-1", "ui-bool", "respondToUi", {"requestId": "confirm-1", "value": False}),
                ("rpc-1", "ui-cancel", "cancelUi", {"requestId": "prompt-2"}),
            ],
        )
        self.assertEqual(
            self.terminal.calls,
            [
                ("terminal-1", "ui-multi", "respondToUi", {"requestId": "7", "value": ["alpha", "beta"]}),
            ],
        )

    def test_terminal_session_removal_reports_removed_and_missing(self):
        status, _, payload = self.post("/api/sessions/terminal-1/remove", {"requestId": "remove-1"})
        self.assertEqual((status, payload), (200, {"status": "removed"}))
        status, _, payload = self.post("/api/sessions/rpc-1/remove", {"requestId": "remove-rpc"})
        self.assertEqual((status, payload), (409, {"error": "session is not removable"}))
        status, _, payload = self.post("/api/sessions/missing/remove", {"requestId": "remove-2"})
        self.assertEqual((status, payload), (404, {"error": "session not found"}))
        self.assertEqual(self.terminal.removals, ["terminal-1", "missing"])

    def test_origin_csrf_and_authentication_rejections_stop_dispatch(self):
        status, _, payload = self.request("GET", "/api/sessions")
        self.assertEqual((status, payload), (401, {"error": "authentication required"}))

        status, _, payload = self.request(
            "POST",
            "/api/sessions/rpc-1/interrupt",
            {"requestId": "bad-origin"},
            {**self.authenticated_headers(), "Origin": "https://attacker.invalid"},
        )
        self.assertEqual((status, payload), (403, {"error": "request origin is not allowed"}))

        headers = self.authenticated_headers()
        headers.pop("X-OMP-Deck-CSRF")
        status, _, payload = self.request("POST", "/api/sessions/rpc-1/interrupt", {"requestId": "no-csrf"}, headers)
        self.assertEqual(status, 403)
        self.assertEqual(payload, {"error": "authentication and CSRF token required"})

        status, _, payload = self.request(
            "POST",
            "/api/sessions/rpc-1/interrupt",
            {"requestId": "no-auth"},
            {"Origin": self.origin, "Sec-Fetch-Site": "same-origin"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(payload, {"error": "authentication and CSRF token required"})
        self.assertEqual(self.rpc.calls, [])

    def test_stop_runtime_closes_rpc_after_terminal_failure(self):
        calls = []

        class Manager:
            def __init__(self, label, failure=None):
                self.label = label
                self.failure = failure

            def close(self):
                calls.append(self.label)
                if self.failure is not None:
                    raise self.failure

        with (
            patch.object(
                server,
                "_terminal_manager",
                Manager("terminal", RuntimeError("terminal failed")),
            ),
            patch.object(server, "_rpc_manager", Manager("RPC")),
        ):
            with self.assertRaisesRegex(RuntimeError, "terminal failed"):
                server.stop_runtime()

        self.assertEqual(calls, ["terminal", "RPC"])


if __name__ == "__main__":
    unittest.main()
