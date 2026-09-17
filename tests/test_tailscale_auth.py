import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from omp_deck import build_identity
from omp_deck.coordinator_server import CoordinatorContext, CoordinatorHTTPServer
from omp_deck.coordinator_settings import CoordinatorSettings
from omp_deck.fleet_protocol import verify_ticket


class CoordinatorTailscaleHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.origin = "https://coordinator.example"
        self.settings = CoordinatorSettings(
            host="127.0.0.1",
            port=0,
            origin=self.origin,
            state_root=Path(self.temp.name) / "state",
            allowed_hosts=("127.0.0.1", "coordinator.example"),
            auth_mode="tailscale",
            tailscale_user="owner@example.com",
        )
        self.context = CoordinatorContext.create(self.settings)
        self.enrollment = self.context.store.add_machine("Workstation", "https://workstation.example")
        self.httpd = CoordinatorHTTPServer(("127.0.0.1", 0), self.context)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 2)
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

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

    def test_identity_login_controls_coordinator_csp_roster_and_ticket_issuance(self):
        identity = {"Tailscale-User-Login": "owner@example.com"}
        browser = {**identity, "Origin": self.origin, "Sec-Fetch-Site": "same-origin"}

        status, _, config = self.request("GET", "/api/client-config")
        self.assertEqual(
            (status, config), (200, {"mode": "coordinator", "authMode": "tailscale", "build": build_identity()})
        )
        self.assertNotIn("owner@example.com", json.dumps(config))
        status, headers, login = self.request("POST", "/api/auth/tailscale", {}, browser)
        self.assertEqual(status, 200)
        cookie = headers["Set-Cookie"].split(";", 1)[0]

        status, headers, _ = self.request(
            "GET",
            "/",
            headers={**identity, "Cookie": cookie},
            parse_json=False,
        )
        self.assertEqual(status, 200)
        self.assertIn("https://workstation.example", headers["Content-Security-Policy"])
        status, headers, _ = self.request(
            "GET",
            "/",
            headers={"Tailscale-User-Login": "other@example.com", "Cookie": cookie},
            parse_json=False,
        )
        self.assertEqual(status, 200)
        self.assertNotIn("https://workstation.example", headers["Content-Security-Policy"])

        access_headers = {
            **browser,
            "Cookie": cookie,
            "X-OMP-Deck-CSRF": login["csrfToken"],
        }
        status, _, response = self.request("POST", "/api/fleet/access", {}, access_headers)
        self.assertEqual(status, 200)
        grant = response["machines"][0]
        claims = verify_ticket(grant["accessToken"], self.enrollment, now=grant["expiresAt"] - 1)
        self.assertEqual(claims["deviceId"], login["deviceId"])

        for denied in (
            {key: value for key, value in access_headers.items() if key != "Tailscale-User-Login"},
            {**access_headers, "Tailscale-User-Login": "other@example.com"},
            {**access_headers, "X-OMP-Deck-CSRF": "wrong"},
        ):
            status, _, _ = self.request("POST", "/api/fleet/access", {}, denied)
            self.assertIn(status, {401, 403})

        secret = self.context.auth.begin_pairing()
        state_path = self.settings.state_root / "auth.json"
        devices_before = json.loads(state_path.read_text())["devices"]
        status, headers, _ = self.request("POST", "/api/auth/pair", {"token": secret.token}, browser)
        devices_after = json.loads(state_path.read_text())["devices"]
        self.assertEqual(status, 403)
        self.assertIsNone(headers.get("Set-Cookie"))
        self.assertEqual(devices_after, devices_before)


if __name__ == "__main__":
    unittest.main()
