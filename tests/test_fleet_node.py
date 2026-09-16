import argparse
import base64
import contextlib
import http.client
import io
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from omp_deck import _sse, fleet_cli, fleet_protocol, server
from omp_deck._fleet_trust import FleetTrustStore
from omp_deck.auth_policy import AuthPolicy
from omp_deck.fleet_protocol import issue_ticket


def _encoded(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


class FleetNodeBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state_root = Path(self.directory.name) / "state"
        settings = SimpleNamespace(
            allowed_hosts=("127.0.0.1",),
            state_root=self.state_root,
            auth_mode="pairing",
            tailscale_user=None,
        )
        self.settings_patch = patch.object(server, "SETTINGS", settings)
        self.settings_patch.start()
        self.addCleanup(self.settings_patch.stop)
        self.policy_patch = patch.object(server, "AUTH_POLICY", AuthPolicy())
        self.policy_patch.start()
        self.addCleanup(self.policy_patch.stop)
        for name in ("_auth_manager", "_fleet_trust_store"):
            patcher = patch.object(server, name, None)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.model_patch = patch.object(server, "_fleet_model", server.FleetModel())
        self.model_patch.start()
        self.addCleanup(self.model_patch.stop)

        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)
        self.host = f"127.0.0.1:{self.httpd.server_port}"
        self.coordinator_origin = "http://localhost:8790"

        key = Ed25519PrivateKey.generate()
        private_bytes = key.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
        public_bytes = key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        self.private_key = _encoded(private_bytes)
        self.enrollment = {
            "version": 1,
            "machineId": "1" * 32,
            "name": "Test node",
            "coordinatorOrigin": self.coordinator_origin,
            "machineOrigin": f"http://{self.host}",
            "publicKey": _encoded(public_bytes),
        }
        FleetTrustStore(self.state_root).join(self.enrollment)
        self.now = 1_000_000
        self.token, self.expires_at = issue_ticket(
            self.private_key,
            issuer=self.coordinator_origin,
            machine_id=self.enrollment["machineId"],
            device_id="2" * 32,
            now=self.now,
        )
        self.clock_patch = patch.object(fleet_protocol.time, "time", side_effect=lambda: self.now)
        self.clock_patch.start()
        self.addCleanup(self.clock_patch.stop)

    def _stop_server(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(2)

    def request(self, method, path, *, headers=None, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=2)
        payload = None if body is None else json.dumps(body)
        request_headers = {"Host": self.host, **(headers or {})}
        if payload is not None:
            request_headers["Content-Type"] = "application/json"
        connection.request(method, path, body=payload, headers=request_headers)
        response = connection.getresponse()
        raw = response.read()
        result = response.status, response.headers, json.loads(raw) if raw else None
        connection.close()
        return result

    def bearer_headers(self, token=None, origin=None):
        return {
            "Authorization": f"Bearer {self.token if token is None else token}",
            "Origin": self.coordinator_origin if origin is None else origin,
        }

    def test_bad_bearer_never_falls_back_to_valid_local_cookie(self):
        credential = server.auth_manager().begin_pairing()
        paired = server.auth_manager().pair(credential.token, source="test")

        status, headers, payload = self.request(
            "GET",
            "/api/blob/missing",
            headers={
                **self.bearer_headers("invalid"),
                "Cookie": paired.set_cookie.split(";", 1)[0],
            },
        )

        self.assertEqual(status, 401)
        self.assertEqual(payload, {"error": "authentication required"})
        self.assertEqual(headers["Access-Control-Allow-Origin"], self.coordinator_origin)

    def test_bearer_requires_exact_pinned_origin_and_machine_host(self):
        status, headers, payload = self.request(
            "GET",
            "/api/blob/missing",
            headers=self.bearer_headers(),
        )
        self.assertEqual(status, 404)
        self.assertEqual(payload, {"error": "image not found"})
        self.assertEqual(headers["Access-Control-Allow-Origin"], self.coordinator_origin)

        status, headers, _ = self.request(
            "GET",
            "/api/blob/missing",
            headers=self.bearer_headers(origin="http://localhost:8791"),
        )
        self.assertEqual(status, 401)
        self.assertIsNone(headers.get("Access-Control-Allow-Origin"))

        status, headers, _ = self.request(
            "GET",
            "/api/blob/missing",
            headers={**self.bearer_headers(), "Host": "127.0.0.1"},
        )
        self.assertEqual(status, 401)
        self.assertIsNone(headers.get("Access-Control-Allow-Origin"))

    def test_valid_bearer_authorizes_mutation_without_local_csrf(self):
        status, headers, payload = self.request(
            "POST",
            "/api/sessions/missing/interrupt",
            headers=self.bearer_headers(),
            body={"requestId": "fleet-request"},
        )

        self.assertEqual(status, 404)
        self.assertEqual(payload, {"error": "session not found"})
        self.assertEqual(headers["Access-Control-Allow-Origin"], self.coordinator_origin)

    def test_preflight_is_limited_to_enrolled_origin_routes_and_headers(self):
        accepted = {
            "Origin": self.coordinator_origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization, content-type",
            "Access-Control-Request-Private-Network": "true",
        }
        status, headers, payload = self.request(
            "OPTIONS",
            "/api/sessions/local/interrupt",
            headers=accepted,
        )
        self.assertEqual(status, 204)
        self.assertIsNone(payload)
        self.assertEqual(headers["Access-Control-Allow-Origin"], self.coordinator_origin)
        self.assertEqual(headers["Access-Control-Allow-Private-Network"], "true")

        status, headers, _ = self.request(
            "OPTIONS",
            "/api/sessions/local/interrupt",
            headers={**accepted, "Access-Control-Request-Headers": "authorization, x-secret"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), self.coordinator_origin)

        status, headers, _ = self.request(
            "OPTIONS",
            "/api/sessions/local/interrupt",
            headers={**accepted, "Origin": "http://localhost:8791"},
        )
        self.assertEqual(status, 403)
        self.assertIsNone(headers.get("Access-Control-Allow-Origin"))

    def test_preflight_without_requested_headers_is_valid(self):
        status, headers, _ = self.request(
            "OPTIONS",
            "/api/sessions",
            headers={
                "Origin": self.coordinator_origin,
                "Access-Control-Request-Method": "GET",
            },
        )
        self.assertEqual(status, 204)
        self.assertEqual(headers["Access-Control-Allow-Origin"], self.coordinator_origin)

    def test_nested_pairing_input_returns_a_client_error(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=2)
        self.addCleanup(connection.close)
        connection.request(
            "POST",
            "/api/auth/pair",
            body='{"token":' + "[" * 1200 + "]" * 1200 + "}",
            headers={"Host": self.host, "Origin": f"http://{self.host}", "Content-Type": "application/json"},
        )
        response = connection.getresponse()
        # Supported Python decoders differ in how much nesting they accept.
        # Either parsing or pairing rejects it with a structured client error.
        self.assertIn(response.status, {400, 401})
        self.assertEqual(set(json.loads(response.read())), {"error"})

    def test_simultaneous_viewers_share_trust_without_authentication_failures(self):
        def request_blob(_):
            return self.request("GET", "/api/blob/missing", headers=self.bearer_headers())[0]

        with ThreadPoolExecutor(max_workers=8) as viewers:
            statuses = list(viewers.map(request_blob, range(64)))
        self.assertEqual(statuses, [404] * 64)

    def test_live_trust_removal_revokes_ticket_without_restart(self):
        self.assertEqual(
            self.request("GET", "/api/blob/missing", headers=self.bearer_headers())[0],
            404,
        )
        self.assertTrue(FleetTrustStore(self.state_root).leave())

        status, _, payload = self.request("GET", "/api/blob/missing", headers=self.bearer_headers())
        self.assertEqual(status, 401)
        self.assertEqual(payload, {"error": "authentication required"})

    def test_expired_ticket_closes_stream_before_delivering_new_event(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=2)
        connection.request("GET", "/api/events", headers={"Host": self.host, **self.bearer_headers()})
        response = connection.getresponse()
        self.addCleanup(connection.close)
        self.assertEqual(response.status, 200)
        while True:
            line = response.fp.readline()
            self.assertTrue(line, "SSE connection closed before its initial frame")
            if line == b"\n":
                break

        self.now = self.expires_at
        server._fleet_model.journal.publish(
            snapshot_generation=1,
            event_type="change",
            payload={"private": "must not arrive"},
        )

        self.assertEqual(response.fp.readline(), b"")

    def test_ticket_expiry_stops_remaining_replay_frames(self):
        journal = server._fleet_model.journal
        for generation in (1, 2):
            journal.publish(
                snapshot_generation=generation,
                event_type="change",
                payload={"generation": generation},
            )
        write_event = _sse._write_event

        def expire_after_frame(*args):
            write_event(*args)
            self.now = self.expires_at

        connection = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=2)
        self.addCleanup(connection.close)
        with patch.object(_sse, "_write_event", side_effect=expire_after_frame):
            connection.request(
                "GET",
                "/api/events",
                headers={
                    "Host": self.host,
                    "Last-Event-ID": journal.cursor(0),
                    **self.bearer_headers(),
                },
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            frame = []
            while line := response.fp.readline():
                frame.append(line)
                if line == b"\n":
                    break
            self.assertIn(b'"generation":1', b"".join(frame))
            self.assertEqual(response.fp.readline(), b"")


class FleetCliBoundaryTests(unittest.TestCase):
    def test_join_status_and_leave_only_manage_node_trust_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            config = root / "config.json"
            bundle = root / "enrollment.json"
            key = (
                Ed25519PrivateKey.generate()
                .public_key()
                .public_bytes(
                    serialization.Encoding.Raw,
                    serialization.PublicFormat.Raw,
                )
            )
            config.write_text(json.dumps({"state_root": str(state)}), encoding="utf-8")
            bundle.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "machineId": "a" * 32,
                        "name": "Node",
                        "coordinatorOrigin": "http://localhost:8790",
                        "machineOrigin": "http://127.0.0.1:8787",
                        "publicKey": _encoded(key),
                    }
                ),
                encoding="utf-8",
            )
            config.chmod(0o600)
            bundle.chmod(0o600)
            parser = argparse.ArgumentParser()
            fleet_cli.add_parser(parser.add_subparsers(dest="command", required=True))
            args = parser.parse_args(["fleet", "join", "--file", str(bundle), "--config", str(config)])

            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(fleet_cli.run(args), 0)

            self.assertTrue((state / "fleet-node.json").is_file())
            self.assertFalse((state / "auth.json").exists())
            self.assertEqual(FleetTrustStore(state).load()["machineId"], "a" * 32)
            with self.assertRaisesRegex(RuntimeError, "leave first"):
                FleetTrustStore(state).join(
                    {**FleetTrustStore(state).load(), "coordinatorOrigin": "http://localhost:8791"}
                )

            status_args = parser.parse_args(["fleet", "status", "--config", str(config)])
            status_output = io.StringIO()
            with contextlib.redirect_stdout(status_output):
                self.assertEqual(fleet_cli.run(status_args), 0)
            self.assertTrue(json.loads(status_output.getvalue())["enrolled"])

            leave_args = parser.parse_args(["fleet", "leave", "--config", str(config)])
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(fleet_cli.run(leave_args), 0)
            self.assertFalse((state / "fleet-node.json").exists())


if __name__ == "__main__":
    unittest.main()
