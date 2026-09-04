import http.client
import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from omp_deck import attachments, server


class HttpSecurityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        settings = SimpleNamespace(
            allowed_hosts=("127.0.0.1",),
            state_root=Path(self.directory.name),
        )
        self.settings_patch = patch.object(server, "SETTINGS", settings)
        self.settings_patch.start()
        self.addCleanup(self.settings_patch.stop)
        self.blob_root = Path(self.directory.name) / "blobs"
        self.blob_root.mkdir()
        self.blob_patch = patch.object(
            attachments,
            "BLOB_ROOT",
            self.blob_root,
        )
        self.blob_patch.start()
        self.addCleanup(self.blob_patch.stop)
        server._auth_manager = None
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

    def test_pairing_requires_same_origin_and_issues_secure_cookie(self):
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
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("Secure", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])

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
