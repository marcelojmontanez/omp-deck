import hashlib
import http.client
import json
import tempfile
import threading
import unittest
from email.header import Header
from email.utils import parsedate_to_datetime
from http.cookies import SimpleCookie
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from omp_deck import attachments, server
from omp_deck.auth import AuthManager
from omp_deck.auth_policy import AuthPolicy


class Clock:
    def __init__(self, value=1_700_000_000):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class HttpSecurityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        settings = SimpleNamespace(
            allowed_hosts=("127.0.0.1",),
            state_root=Path(self.directory.name),
            auth_mode="pairing",
            tailscale_user=None,
        )
        self.settings = settings
        self.settings_patch = patch.object(server, "SETTINGS", settings)
        self.settings_patch.start()
        self.addCleanup(self.settings_patch.stop)
        self.policy_patch = patch.object(server, "AUTH_POLICY", AuthPolicy())
        self.policy_patch.start()
        self.addCleanup(self.policy_patch.stop)
        self.blob_root = Path(self.directory.name) / "blobs"
        self.blob_root.mkdir()
        self.blob_patch = patch.object(
            attachments,
            "BLOB_ROOT",
            self.blob_root,
        )
        self.blob_patch.start()
        self.addCleanup(self.blob_patch.stop)
        self.wall = Clock()
        server._auth_manager = AuthManager(settings.state_root, session_ttl=60, now=self.wall)
        self.addCleanup(setattr, server, "_auth_manager", None)
        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.host = f"127.0.0.1:{self.httpd.server_port}"
        self.origin = f"http://{self.host}"

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port)
        payload = None if body is None else json.dumps(body)
        request_headers = {"Host": self.host, **(headers or {})}
        if payload is not None:
            request_headers["Content-Type"] = "application/json"
        connection.request(method, path, body=payload, headers=request_headers)
        response = connection.getresponse()
        data = json.loads(response.read())
        result = response.status, response.headers, data
        connection.close()
        return result

    def request_with_duplicate_identity(self, path, body, values):
        connection = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port)
        payload = json.dumps(body)
        connection.putrequest("POST", path, skip_host=True)
        connection.putheader("Host", self.host)
        connection.putheader("Origin", self.origin)
        connection.putheader("Sec-Fetch-Site", "same-origin")
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", str(len(payload)))
        for value in values:
            connection.putheader("Tailscale-User-Login", value)
        connection.endheaders(payload.encode())
        response = connection.getresponse()
        result = response.status, response.headers, json.loads(response.read())
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
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        return cookie, payload["csrfToken"]

    def test_http_startup_does_not_require_reverse_dns(self):
        with patch("socket.getfqdn", side_effect=OSError("reverse DNS unavailable")):
            httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=2)
            try:
                connection.request("GET", "/api/sessions")
                response = connection.getresponse()
                self.assertEqual(response.status, 401)
                self.assertEqual(json.loads(response.read()), {"error": "authentication required"})
            finally:
                connection.close()
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)

    def test_private_routes_require_authentication_and_no_store(self):
        status, headers, payload = self.request("GET", "/api/sessions")

        self.assertEqual(status, 401)
        self.assertEqual(payload, {"error": "authentication required"})
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")

    def test_blob_delivery_requires_authentication_and_is_not_cached(self):
        body = b"\x89PNG\r\n\x1a\nprivate"
        digest = hashlib.sha256(body).hexdigest()
        (self.blob_root / digest).write_bytes(body)

        status, _, payload = self.request("GET", f"/api/blob/{digest}")
        self.assertEqual(status, 401)
        self.assertEqual(payload, {"error": "authentication required"})

        cookie, _ = self.pair()
        connection = http.client.HTTPConnection(
            "127.0.0.1",
            self.httpd.server_port,
        )
        connection.request(
            "GET",
            f"/api/blob/{digest}",
            headers={"Host": self.host, "Cookie": cookie},
        )
        response = connection.getresponse()
        delivered = response.read()
        headers = response.headers
        connection.close()

        self.assertEqual(response.status, 200)
        self.assertEqual(delivered, body)
        self.assertEqual(headers["Content-Type"], "image/png")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")

    def test_pairing_requires_same_origin_and_issues_persistent_secure_cookie(self):
        self.wall.advance(0.75)
        secret = server.auth_manager().begin_pairing()
        status, _, _ = self.request("POST", "/api/auth/pair", {"token": secret.token})
        self.assertEqual(status, 403)

        status, headers, payload = self.request(
            "POST",
            "/api/auth/pair",
            {"token": secret.token},
            {"Origin": self.origin, "Sec-Fetch-Site": "same-origin"},
        )

        self.assertEqual(status, 200)
        self.assertTrue(payload["authenticated"])
        cookie = SimpleCookie()
        cookie.load(headers["Set-Cookie"])
        morsel = cookie["omp_deck_device"]
        self.assertEqual(int(morsel["max-age"]), 59)
        self.assertEqual(
            int(parsedate_to_datetime(morsel["expires"]).timestamp()),
            int(self.wall.value) + 60,
        )
        self.assertTrue(morsel["httponly"])
        self.assertTrue(morsel["secure"])
        self.assertEqual(morsel["samesite"], "Strict")

    def test_session_check_adopts_cookie_without_renewing_expiry_across_restart(self):
        cookie, _ = self.pair()
        credential = cookie.split("=", 1)[1]
        original_expiry = server.auth_manager().authenticate(credential).expires_at
        self.wall.advance(19)
        server._auth_manager = AuthManager(self.settings.state_root, session_ttl=60, now=self.wall)

        status, headers, payload = self.request("GET", "/api/auth/session", headers={"Cookie": cookie})

        self.assertEqual(status, 200)
        self.assertTrue(payload["authenticated"])
        adopted = SimpleCookie()
        adopted.load(headers["Set-Cookie"])
        morsel = adopted["omp_deck_device"]
        self.assertEqual(morsel.value, credential)
        self.assertEqual(int(morsel["max-age"]), 41)
        self.assertEqual(int(parsedate_to_datetime(morsel["expires"]).timestamp()), original_expiry)
        self.assertEqual(server.auth_manager().authenticate(credential).expires_at, original_expiry)

        self.wall.advance(41)
        status, headers, payload = self.request("GET", "/api/auth/session", headers={"Cookie": cookie})
        self.assertEqual((status, payload), (200, {"authenticated": False}))
        self.assertIsNone(headers.get("Set-Cookie"))

    def test_mutations_require_session_bound_csrf(self):
        cookie, csrf = self.pair()
        base_headers = {
            "Cookie": cookie,
            "Origin": self.origin,
            "Sec-Fetch-Site": "same-origin",
        }
        status, _, _ = self.request("POST", "/api/auth/logout", {}, base_headers)
        self.assertEqual(status, 403)

        status, headers, payload = self.request(
            "POST",
            "/api/auth/logout",
            {},
            {**base_headers, "X-OMP-Deck-CSRF": csrf},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"ok": True})
        self.assertIn("Max-Age=0", headers["Set-Cookie"])

        status, _, _ = self.request("GET", "/api/sessions", headers={"Cookie": cookie})
        self.assertEqual(status, 401)

    def test_revoke_all_rejects_every_cookie_and_clears_the_present_cookie(self):
        first_cookie, first_csrf = self.pair()
        second_cookie, _ = self.pair()
        request_headers = {
            "Cookie": first_cookie,
            "Origin": self.origin,
            "Sec-Fetch-Site": "same-origin",
            "X-OMP-Deck-CSRF": first_csrf,
        }

        status, headers, payload = self.request("POST", "/api/auth/revoke-all", {}, request_headers)

        self.assertEqual((status, payload), (200, {"ok": True}))
        cleared = SimpleCookie()
        cleared.load(headers["Set-Cookie"])
        self.assertEqual(cleared["omp_deck_device"]["max-age"], "0")
        for cookie in (first_cookie, second_cookie):
            status, session_headers, payload = self.request(
                "GET",
                "/api/auth/session",
                headers={"Cookie": cookie},
            )
            self.assertEqual((status, payload), (200, {"authenticated": False}))
            self.assertIsNone(session_headers.get("Set-Cookie"))

    def test_tailscale_login_reuses_cookie_and_requires_identity_and_csrf_on_every_request(self):
        cookie, _ = self.pair()
        credential = cookie.split("=", 1)[1]
        first_device = server.auth_manager().authenticate(credential).device_id
        self.settings.auth_mode = "tailscale"
        self.settings.tailscale_user = "owner@example.com"
        server.AUTH_POLICY = AuthPolicy("tailscale", "owner@example.com")
        identity = {"Tailscale-User-Login": "owner@example.com"}
        trusted = {**identity, "Origin": self.origin, "Sec-Fetch-Site": "same-origin"}
        status, _, adopted = self.request(
            "GET",
            "/api/auth/session",
            headers={**identity, "Cookie": cookie},
        )
        self.assertEqual((status, adopted["deviceId"]), (200, first_device))

        status, headers, login = self.request(
            "POST",
            "/api/auth/tailscale",
            {},
            {**trusted, "Cookie": cookie},
        )
        self.assertEqual((status, login["deviceId"]), (200, first_device))
        self.assertTrue(login["authenticated"])
        self.assertEqual(headers["Set-Cookie"].split(";", 1)[0], cookie)

        status, _, repeated = self.request(
            "POST",
            "/api/auth/tailscale",
            {},
            {**trusted, "Cookie": cookie},
        )
        self.assertEqual((status, repeated["deviceId"]), (200, first_device))

        status, _, session = self.request(
            "GET",
            "/api/auth/session",
            headers={**identity, "Cookie": cookie},
        )
        self.assertEqual((status, session["deviceId"]), (200, first_device))
        for headers in (
            {"Cookie": cookie},
            {"Cookie": cookie, "Tailscale-User-Login": "other@example.com"},
        ):
            status, _, payload = self.request("GET", "/api/auth/session", headers=headers)
            self.assertEqual((status, payload), (200, {"authenticated": False}))

        mutation = {
            **trusted,
            "Cookie": cookie,
        }
        status, _, _ = self.request("POST", "/api/auth/logout", {}, mutation)
        self.assertEqual(status, 403)
        status, _, _ = self.request(
            "POST",
            "/api/auth/logout",
            {},
            {**mutation, "X-OMP-Deck-CSRF": login["csrfToken"], "Tailscale-User-Login": "other@example.com"},
        )
        self.assertEqual(status, 403)
        status, headers, payload = self.request(
            "POST",
            "/api/auth/logout",
            {},
            {**mutation, "X-OMP-Deck-CSRF": login["csrfToken"]},
        )
        self.assertEqual((status, payload), (200, {"ok": True}))
        self.assertIn("Max-Age=0", headers["Set-Cookie"])

    def test_rfc2047_utf8_identity_issues_cookie_usable_for_session_authentication(self):
        owner = "ü" * 500 + "@x"
        encoded = " ".join(part.strip() for part in Header(owner, "utf-8").encode().splitlines())
        self.settings.auth_mode = "tailscale"
        self.settings.tailscale_user = owner
        server.AUTH_POLICY = AuthPolicy("tailscale", owner)
        identity = {"Tailscale-User-Login": encoded}
        trusted = {**identity, "Origin": self.origin, "Sec-Fetch-Site": "same-origin"}

        status, headers, login = self.request("POST", "/api/auth/tailscale", {}, trusted)
        self.assertEqual(status, 200)
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        status, _, session = self.request(
            "GET",
            "/api/auth/session",
            headers={**identity, "Cookie": cookie},
        )
        self.assertEqual(
            (status, session),
            (
                200,
                {
                    "authenticated": True,
                    "deviceId": login["deviceId"],
                    "csrfToken": login["csrfToken"],
                },
            ),
        )

    def test_tailscale_login_rejects_untrusted_inputs_without_consuming_pairing_secret(self):
        self.settings.auth_mode = "tailscale"
        self.settings.tailscale_user = "owner@example.com"
        identity = {"Tailscale-User-Login": "owner@example.com"}
        trusted = {**identity, "Origin": self.origin, "Sec-Fetch-Site": "same-origin"}
        server.AUTH_POLICY = AuthPolicy("tailscale", "owner@example.com")
        secret = server.auth_manager().begin_pairing()

        denied = (
            ({}, {"Origin": self.origin, "Sec-Fetch-Site": "same-origin"}),
            ({}, {**trusted, "Tailscale-User-Login": "other@example.com"}),
            ({}, {**trusted, "Tailscale-User-Login": "=?UTF-8?B?not-base64!?="}),
            ({}, {**trusted, "Origin": "http://attacker.example"}),
            ({"identity": "owner@example.com"}, trusted),
            ([], trusted),
        )
        for body, headers in denied:
            with self.subTest(body=body, headers=headers):
                status, response_headers, _ = self.request("POST", "/api/auth/tailscale", body, headers)
                self.assertIn(status, {400, 403})
                self.assertIsNone(response_headers.get("Set-Cookie"))

        status, headers, _ = self.request("POST", "/api/auth/pair", {"token": secret.token}, trusted)
        self.assertEqual(status, 403)
        self.assertIsNone(headers.get("Set-Cookie"))
        state = json.loads((self.settings.state_root / "auth.json").read_text())
        self.assertEqual(state["devices"], {})
        paired = server.auth_manager().pair(secret.token, source="proof-pairing-token-was-not-consumed")
        self.assertEqual(server.auth_manager().authenticate(paired.credential).device_id, paired.device.device_id)

        status, headers, _ = self.request_with_duplicate_identity(
            "/api/auth/tailscale",
            {},
            ("owner@example.com", "owner@example.com"),
        )
        self.assertEqual(status, 403)
        self.assertIsNone(headers.get("Set-Cookie"))

    def test_client_config_publishes_build_without_owner_identity(self):
        status, _, payload = self.request("GET", "/api/client-config")
        self.assertEqual(
            (status, payload), (200, {"mode": "local", "authMode": "pairing", "build": server.build_identity()})
        )

        self.settings.auth_mode = "tailscale"
        self.settings.tailscale_user = "private-owner@example.com"
        status, _, payload = self.request("GET", "/api/client-config")

        self.assertEqual(
            (status, payload), (200, {"mode": "local", "authMode": "tailscale", "build": server.build_identity()})
        )
        self.assertNotIn("private-owner@example.com", json.dumps(payload))

    def test_invalid_host_is_rejected_before_static_or_api_access(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port)
        connection.request("GET", "/healthz", headers={"Host": "attacker.example"})
        response = connection.getresponse()
        payload = json.loads(response.read())
        connection.close()

        self.assertEqual(response.status, 400)
        self.assertEqual(payload, {"error": "invalid host"})


if __name__ == "__main__":
    unittest.main()
