import contextlib
import http.client
import io
import json
import os
import socket
import stat
import tempfile
import threading
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from omp_deck.auth import AuthenticationFailed
from omp_deck.coordinator_cli import run as run_coordinator
from omp_deck.coordinator_server import CoordinatorContext, CoordinatorHTTPServer
from omp_deck.coordinator_settings import CoordinatorSettings, load_coordinator_settings
from omp_deck.coordinator_store import CoordinatorStateError, CoordinatorStore
from omp_deck.fleet_protocol import verify_ticket


class CoordinatorStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "state"
        self.origin = "https://coordinator.example"

    def test_state_is_private_and_duplicate_machine_identity_is_rejected(self):
        store = CoordinatorStore(self.root, self.origin)
        enrollment = store.add_machine("Workstation", "https://workstation.example")
        state_path = self.root / "fleet-coordinator.json"
        state = json.loads(state_path.read_text())

        self.assertEqual(state["version"], 1)
        self.assertEqual(len(state["machines"]), 1)
        self.assertNotIn("privateKey", enrollment)
        self.assertEqual(
            set(enrollment),
            {"version", "machineId", "name", "coordinatorOrigin", "machineOrigin", "publicKey"},
        )
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(state_path.stat().st_mode), 0o600)
        with self.assertRaisesRegex(ValueError, "name"):
            store.add_machine("workstation", "https://second.example")
        with self.assertRaisesRegex(ValueError, "origin"):
            store.add_machine("Second", "https://workstation.example")

    def test_machine_limit_is_enforced_at_the_mutating_boundary(self):
        store = CoordinatorStore(self.root, self.origin)
        for index in range(32):
            store.add_machine(f"node-{index}", f"https://node-{index}.example")
        with self.assertRaisesRegex(ValueError, "at most 32"):
            store.add_machine("one-too-many", "https://excess.example")
        self.assertEqual(len(store.machines()), 32)

    def test_corrupt_state_is_rejected_without_resetting_the_signing_key(self):
        store = CoordinatorStore(self.root, self.origin)
        original_key = store.private_key
        path = self.root / "fleet-coordinator.json"
        corrupt = json.loads(path.read_text())
        corrupt["machines"] = "not-a-list"
        path.write_text(json.dumps(corrupt))
        os.chmod(path, 0o600)

        with self.assertRaisesRegex(CoordinatorStateError, "corrupt"):
            CoordinatorStore(self.root, self.origin)

        unchanged = json.loads(path.read_text())
        self.assertEqual(unchanged["privateKey"], original_key)
        self.assertEqual(unchanged["machines"], "not-a-list")

    @unittest.skipIf(os.name == "nt", "POSIX ownership and mode semantics")
    def test_unsafe_existing_state_root_is_rejected_without_mutation(self):
        self.root.mkdir(mode=0o777)
        self.root.chmod(0o777)
        sentinel = self.root / "sentinel"
        sentinel.write_text("preserve")

        with self.assertRaisesRegex(CoordinatorStateError, "unsafe"):
            CoordinatorStore(self.root, self.origin)

        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o777)
        self.assertEqual(sentinel.read_text(), "preserve")
        self.assertFalse((self.root / "fleet-coordinator.json").exists())

    def test_config_defaults_local_and_requires_explicit_remote_exposure(self):
        config = Path(self.temp.name) / "coordinator.json"
        state = Path(self.temp.name) / "coordinator-state"
        config.write_text(json.dumps({"origin": self.origin, "state_root": str(state)}))
        os.chmod(config, 0o600)

        settings = load_coordinator_settings(config)

        self.assertEqual((settings.host, settings.port), ("127.0.0.1", 8790))
        config.write_text(json.dumps({"host": "0.0.0.0", "origin": self.origin, "state_root": str(state)}))
        os.chmod(config, 0o600)
        with self.assertRaisesRegex(RuntimeError, "explicit allowed_hosts"):
            load_coordinator_settings(config)

    def test_tailscale_auth_configuration_requires_owner_and_loopback_listener(self):
        config = Path(self.temp.name) / "coordinator-auth.json"
        state = Path(self.temp.name) / "coordinator-auth-state"
        base = {"origin": self.origin, "state_root": str(state)}
        for auth in (
            {"auth_mode": "invalid"},
            {"auth_mode": "tailscale"},
            {"auth_mode": "pairing", "tailscale_user": "owner@example.com"},
            {
                "auth_mode": "tailscale",
                "tailscale_user": "owner@example.com",
                "host": "0.0.0.0",
                "allowed_hosts": ["coordinator.example"],
            },
        ):
            with self.subTest(auth=auth):
                config.write_text(json.dumps({**base, **auth}))
                config.chmod(0o600)
                with self.assertRaises(RuntimeError):
                    load_coordinator_settings(config)

    def test_pair_cli_refuses_tailscale_mode_without_creating_auth_state(self):
        config = Path(self.temp.name) / "coordinator-tailscale.json"
        state = Path(self.temp.name) / "coordinator-tailscale-state"
        config.write_text(
            json.dumps(
                {
                    "origin": self.origin,
                    "state_root": str(state),
                    "auth_mode": "tailscale",
                    "tailscale_user": "owner@example.com",
                }
            )
        )
        config.chmod(0o600)

        with self.assertRaises(RuntimeError):
            run_coordinator(Namespace(coordinator_action="pair", config=str(config)))
        self.assertFalse((state / "auth.json").exists())

    def test_machine_add_cli_writes_exclusive_owner_only_public_bundle(self):
        config = Path(self.temp.name) / "coordinator.json"
        output = Path(self.temp.name) / "join.json"
        config.write_text(json.dumps({"origin": self.origin, "state_root": str(self.root)}))
        os.chmod(config, 0o600)
        args = Namespace(
            coordinator_action="machine",
            machine_action="add",
            name="Workstation",
            origin="https://workstation.example",
            out=str(output),
            config=str(config),
        )

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(run_coordinator(args), 0)
        enrollment = json.loads(output.read_text())
        persisted = (self.root / "fleet-coordinator.json").read_text()

        self.assertNotIn("privateKey", enrollment)
        self.assertNotIn(json.loads(persisted)["privateKey"], output.read_text())
        self.assertIn(enrollment["machineId"], stdout.getvalue())
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
        with self.assertRaisesRegex(RuntimeError, "refusing to replace"):
            run_coordinator(args)
        self.assertEqual(json.loads(output.read_text()), enrollment)

    @unittest.skipIf(os.name == "nt", "POSIX symlink creation")
    def test_enrollment_export_refuses_symlink_parent_and_rolls_back_membership(self):
        root = Path(self.temp.name)
        destination = root / "unrelated"
        destination.mkdir(mode=0o700)
        sentinel = destination / "sentinel"
        sentinel.write_text("preserve")
        alias = root / "alias"
        alias.symlink_to(destination, target_is_directory=True)
        config = root / "config.json"
        config.write_text(json.dumps({"origin": self.origin, "state_root": str(self.root)}))
        config.chmod(0o600)
        args = Namespace(
            coordinator_action="machine",
            machine_action="add",
            name="Node",
            origin="https://node.example",
            out=str(alias / "bundle.json"),
            config=str(config),
        )
        with self.assertRaises(OSError):
            run_coordinator(args)
        self.assertEqual(sentinel.read_text(), "preserve")
        self.assertFalse((destination / "bundle.json").exists())
        self.assertEqual(CoordinatorStore(self.root, self.origin).machines(), ())


class CoordinatorHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = CoordinatorSettings(
            host="127.0.0.1",
            port=0,
            origin="https://coordinator.example",
            state_root=Path(self.temp.name) / "state",
            allowed_hosts=("127.0.0.1", "coordinator.example"),
        )
        self.context = CoordinatorContext.create(self.settings)
        self.enrollment = self.context.store.add_machine("Workstation", "https://workstation.example")
        self.httpd = CoordinatorHTTPServer(("127.0.0.1", 0), self.context)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 2)
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.origin = "https://coordinator.example"

    @unittest.skipUnless(socket.has_ipv6, "IPv6 is unavailable")
    def test_ipv6_listener_serves_accepted_configuration(self):
        with socket.socket(socket.AF_INET6) as probe:
            try:
                probe.bind(("::1", 0))
            except OSError:
                self.skipTest("IPv6 loopback is unavailable")
        config = Path(self.temp.name) / "ipv6.json"
        config.write_text(
            json.dumps(
                {
                    "host": "::1",
                    "port": 18799,
                    "origin": "http://[::1]:18799",
                    "state_root": str(Path(self.temp.name) / "ipv6-state"),
                }
            )
        )
        config.chmod(0o600)
        context = CoordinatorContext.create(load_coordinator_settings(config))
        httpd = CoordinatorHTTPServer((context.settings.host, 0), context)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection("::1", httpd.server_port, timeout=2)
            try:
                connection.request("GET", "/api/client-config", headers={"Host": "[::1]:18799"})
                response = connection.getresponse()
                self.assertEqual(
                    (response.status, json.loads(response.read())),
                    (200, {"mode": "coordinator", "authMode": "pairing"}),
                )
            finally:
                connection.close()
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(2)

    def request(self, method, path, body=None, headers=None, *, parse_json=True):
        connection = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=2)
        payload = None if body is None else json.dumps(body)
        request_headers = {"Host": "coordinator.example", **(headers or {})}
        if payload is not None:
            request_headers["Content-Type"] = "application/json"
        connection.request(method, path, body=payload, headers=request_headers)
        response = connection.getresponse()
        raw = response.read()
        result = response.status, response.headers, json.loads(raw) if parse_json else raw
        connection.close()
        return result

    def pair(self):
        secret = self.context.auth.begin_pairing()
        status, headers, payload = self.request(
            "POST",
            "/api/auth/pair",
            {"token": secret.token},
            {"Origin": self.origin, "Sec-Fetch-Site": "same-origin"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(headers["Set-Cookie"].startswith("omp_deck_coordinator_device="))
        return headers["Set-Cookie"].split(";", 1)[0], payload["csrfToken"]

    def test_public_surface_discloses_neither_roster_nor_machine_csp(self):
        status, headers, payload = self.request("GET", "/api/client-config")
        self.assertEqual((status, payload), (200, {"mode": "coordinator", "authMode": "pairing"}))
        self.assertNotIn("workstation.example", headers["Content-Security-Policy"])

        status, headers, _ = self.request("GET", "/", parse_json=False)
        self.assertEqual(status, 200)
        self.assertNotIn("workstation.example", headers["Content-Security-Policy"])

        status, _, payload = self.request("GET", "/api/fleet")
        self.assertEqual((status, payload), (401, {"error": "authentication required"}))
        status, _, payload = self.request("GET", "/api/sessions")
        self.assertEqual((status, payload), (404, {"error": "not found"}))

        cookie, _ = self.pair()
        credential = cookie.split("=", 1)[1]
        status, _, payload = self.request(
            "GET",
            "/api/fleet",
            headers={"Cookie": f"omp_deck_device={credential}"},
        )
        self.assertEqual((status, payload), (401, {"error": "authentication required"}))

        status, headers, _ = self.request("GET", "/", headers={"Cookie": cookie}, parse_json=False)
        self.assertEqual(status, 200)
        self.assertIn("https://workstation.example", headers["Content-Security-Policy"])
        self.assertEqual(headers["Cache-Control"], "no-store")

        status, headers, _ = self.request("GET", "/app.js", headers={"Cookie": cookie}, parse_json=False)
        self.assertEqual(status, 200)
        self.assertNotIn("workstation.example", headers["Content-Security-Policy"])

    def test_older_release_assets_replace_a_newer_cached_response(self):
        status, _, current = self.request("GET", "/app.js", parse_json=False)
        self.assertEqual(status, 200)

        status, headers, delivered = self.request(
            "GET",
            "/app.js",
            headers={"If-Modified-Since": "Thu, 31 Dec 2099 23:59:59 GMT"},
            parse_json=False,
        )

        self.assertEqual(status, 200)
        self.assertEqual(delivered, current)
        self.assertEqual(headers["Cache-Control"], "public, no-cache")

    def test_public_assets_revalidate_by_content_across_upgrade_and_rollback(self):
        assets = Path(self.temp.name) / "assets"
        assets.mkdir()
        script = assets / "app.js"
        script.write_bytes(b"old()")
        with patch("omp_deck._http_common._STATIC_ROOT", assets):
            status, first_headers, body = self.request("GET", "/app.js", parse_json=False)
            self.assertEqual((status, body), (200, b"old()"))
            first_etag = first_headers["ETag"]
            status, headers, body = self.request(
                "GET", "/app.js", headers={"If-None-Match": f'W/{first_etag}, "other"'}, parse_json=False
            )
            self.assertEqual((status, body), (304, b""))
            self.assertEqual(headers["ETag"], first_etag)
            self.assertEqual(headers["Cache-Control"], "public, no-cache")

            script.write_bytes(b"new()")
            os.utime(script, (2, 2))
            status, upgraded_headers, body = self.request(
                "GET", "/app.js", headers={"If-None-Match": first_etag}, parse_json=False
            )
            self.assertEqual((status, body), (200, b"new()"))
            self.assertNotEqual(upgraded_headers["ETag"], first_etag)

            script.write_bytes(b"old()")
            os.utime(script, (1, 1))
            status, restored_headers, body = self.request(
                "GET",
                "/app.js",
                headers={
                    "If-None-Match": upgraded_headers["ETag"],
                    "If-Modified-Since": "Thu, 31 Dec 2099 23:59:59 GMT",
                },
                parse_json=False,
            )
            self.assertEqual((status, body), (200, b"old()"))
            self.assertEqual(restored_headers["ETag"], first_etag)

    def test_html_and_authentication_never_use_public_asset_cache(self):
        cookie, _ = self.pair()
        for path in ("/", "/api/auth/session"):
            with self.subTest(path=path):
                status, headers, _ = self.request(
                    "GET", path, headers={"Cookie": cookie, "If-None-Match": "*"}, parse_json=False
                )
                self.assertEqual(status, 200)
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertIsNone(headers.get("ETag"))

    def test_authenticated_roster_and_access_require_same_origin_csrf(self):
        cookie, csrf = self.pair()
        status, headers, roster = self.request("GET", "/api/fleet", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertEqual(
            roster,
            {
                "version": 1,
                "machines": [
                    {
                        "id": self.enrollment["machineId"],
                        "name": "Workstation",
                        "origin": "https://workstation.example",
                    }
                ],
            },
        )

        base = {"Cookie": cookie, "Origin": self.origin, "Sec-Fetch-Site": "same-origin"}
        status, _, _ = self.request("POST", "/api/fleet/access", {}, base)
        self.assertEqual(status, 403)
        status, _, payload = self.request(
            "POST",
            "/api/fleet/access",
            {},
            {**base, "X-OMP-Deck-CSRF": csrf},
        )
        self.assertEqual(status, 200)
        grant = payload["machines"][0]
        claims = verify_ticket(grant["accessToken"], self.enrollment, now=grant["expiresAt"] - 1)
        self.assertEqual(claims["deviceId"], self.context.auth.authenticate(cookie.split("=", 1)[1]).device_id)

        status, _, _ = self.request(
            "POST",
            "/api/fleet/access",
            {},
            {**base, "Origin": "https://attacker.example", "X-OMP-Deck-CSRF": csrf},
        )
        self.assertEqual(status, 403)

    def test_removal_stops_renewal_while_existing_ticket_has_bounded_lifetime(self):
        cookie, csrf = self.pair()
        headers = {
            "Cookie": cookie,
            "Origin": self.origin,
            "Sec-Fetch-Site": "same-origin",
            "X-OMP-Deck-CSRF": csrf,
        }
        _, _, first = self.request("POST", "/api/fleet/access", {}, headers)
        grant = first["machines"][0]

        self.context.store.remove_machine(self.enrollment["machineId"])
        status, _, second = self.request("POST", "/api/fleet/access", {}, headers)

        self.assertEqual((status, second), (200, {"version": 1, "machines": []}))
        verify_ticket(grant["accessToken"], self.enrollment, now=grant["expiresAt"] - 1)
        with self.assertRaises(AuthenticationFailed):
            verify_ticket(grant["accessToken"], self.enrollment, now=grant["expiresAt"])

    def test_delayed_issuance_cannot_extend_access_after_machine_removal(self):
        cookie, csrf = self.pair()
        material = self.context.store.access_material()
        with patch("omp_deck.fleet_protocol.time.time", return_value=1_000_000) as clock:

            def removed_during_issuance():
                self.context.store.remove_machine(self.enrollment["machineId"])
                clock.return_value = 1_000_070
                return material

            with patch.object(self.context.store, "access_material", side_effect=removed_during_issuance):
                status, _, response = self.request(
                    "POST",
                    "/api/fleet/access",
                    {},
                    {"Cookie": cookie, "Origin": self.origin, "X-OMP-Deck-CSRF": csrf},
                )
        self.assertEqual(status, 200)
        with self.assertRaises(AuthenticationFailed):
            verify_ticket(response["machines"][0]["accessToken"], self.enrollment, now=1_000_070)

    def test_health_reports_persisted_corruption_with_booleans_only(self):
        path = self.settings.state_root / "fleet-coordinator.json"
        path.write_text("not-json")
        os.chmod(path, 0o600)

        status, _, payload = self.request("GET", "/healthz")

        self.assertEqual(status, 503)
        self.assertEqual(payload, {"ok": False, "configuration": True, "authentication": True, "fleet": False})
        self.assertTrue(all(type(value) is bool for value in payload.values()))


if __name__ == "__main__":
    unittest.main()
