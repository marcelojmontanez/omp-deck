import base64
import http.client
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from omp_deck import server
from omp_deck.attachments import MAX_IMAGE_BYTES
from omp_deck.auth_policy import AuthPolicy
from omp_deck.draft_store import (
    RETENTION_MS,
    DraftCapacityError,
    DraftConflictError,
    DraftCorruptionError,
    DraftStore,
    DraftStoreError,
)
from omp_deck.fleet_model import FleetModel

_PNG = b"\x89PNG\r\n\x1a\nprivate-draft-image"
_IMAGE = {
    "data": base64.b64encode(_PNG).decode("ascii"),
    "mimeType": "image/png",
    "name": "private.png",
}


class _Clock:
    def __init__(self, value=1_700_000_000.0):
        self.value = value

    def __call__(self):
        return self.value


class DraftStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state_root = Path(self.temporary.name) / "state"
        self.state_root.mkdir(mode=0o700)
        self.clock = _Clock()
        revisions = iter(character * 16 for character in "abcdefghijklmnopqrstuvwxyz")
        self.store = DraftStore(
            self.state_root,
            clock=self.clock,
            revision_factory=lambda: next(revisions),
        )

    def test_opt_in_cas_reload_and_image_preservation_are_durable(self):
        self.assertEqual(
            self.store.get("session-1"),
            {
                "enabled": False,
                "revision": None,
                "text": "",
                "images": [],
                "updatedAt": None,
                "expiresAt": None,
            },
        )
        created = self.store.save(
            "session-1",
            revision=None,
            enabled=True,
            text="first",
            images=[_IMAGE],
        )
        self.assertNotIn("images", created)
        sidecar = next(path for path in self.store.root.glob("*.json") if path.name != "manifest.json")
        before = (sidecar.stat().st_ino, sidecar.stat().st_mtime_ns, sidecar.read_bytes())

        updated = self.store.save(
            "session-1",
            revision=created["revision"],
            enabled=True,
            text="second",
        )
        self.assertNotIn("images", updated)
        after = (sidecar.stat().st_ino, sidecar.stat().st_mtime_ns, sidecar.read_bytes())
        self.assertEqual(after, before)

        recovered = DraftStore(self.state_root, clock=self.clock).get("session-1")
        self.assertEqual((recovered["text"], recovered["images"]), ("second", [_IMAGE]))
        with self.assertRaises(DraftConflictError) as conflict:
            self.store.save(
                "session-1",
                revision=created["revision"],
                enabled=True,
                text="stale",
            )
        self.assertEqual(conflict.exception.draft, recovered)
        self.assertEqual(self.store.get("session-1"), recovered)

    def test_explicit_image_clear_disable_and_expiry_remove_private_content(self):
        saved = self.store.save(
            "session-1",
            revision=None,
            enabled=True,
            text="recover me",
            images=[_IMAGE],
        )
        cleared = self.store.save(
            "session-1",
            revision=saved["revision"],
            enabled=True,
            text="text only",
            images=[],
        )
        self.assertEqual(self.store.get("session-1")["images"], [])
        self.assertEqual(list(self.store.root.glob("[0-9a-f]*.json")), [])
        with self.assertRaisesRegex(ValueError, "disabled drafts"):
            self.store.save(
                "session-1",
                revision=cleared["revision"],
                enabled=False,
                text="must not persist",
            )
        disabled = self.store.save(
            "session-1",
            revision=cleared["revision"],
            enabled=False,
            text="",
        )
        self.assertFalse(disabled["enabled"])
        self.assertEqual(self.store.get("session-1")["revision"], disabled["revision"])

        enabled = self.store.save(
            "session-1",
            revision=disabled["revision"],
            enabled=True,
            text="expires",
            images=[_IMAGE],
        )
        self.clock.value += RETENTION_MS / 1000
        expired = self.store.get("session-1")
        self.assertEqual((expired["enabled"], expired["revision"]), (False, None))
        self.assertEqual(list(self.store.root.glob("[0-9a-f]*.json")), [])
        with self.assertRaises(DraftConflictError) as conflict:
            self.store.save(
                "session-1",
                revision=enabled["revision"],
                enabled=True,
                text="stale after expiry",
            )
        self.assertIsNone(conflict.exception.draft["revision"])

    def test_record_and_image_storage_bounds_and_explicit_removal(self):
        capacity_root = self.state_root.parent / "record-capacity"
        capacity_root.mkdir(mode=0o700)
        records = DraftStore(capacity_root, max_records=1)
        records.save("one", revision=None, enabled=True, text="kept", images=[_IMAGE])
        with self.assertRaises(DraftCapacityError):
            records.save("two", revision=None, enabled=False, text="")
        self.assertTrue(records.remove("one"))
        self.assertFalse(records.remove("one"))
        self.assertIsNone(records.get("one")["revision"])
        self.assertEqual(list(records.root.glob("[0-9a-f]*.json")), [])

        image_root = self.state_root.parent / "image-capacity"
        image_root.mkdir(mode=0o700)
        images = DraftStore(image_root, max_storage_bytes=1)
        with self.assertRaises(DraftCapacityError):
            images.save("one", revision=None, enabled=True, text="", images=[_IMAGE])

    @unittest.skipIf(os.name == "nt", "symlink creation requires elevated privileges on Windows")
    def test_hostile_paths_permissions_and_unknown_entries_are_refused(self):
        target_root = self.state_root.parent / "target-state"
        target_root.mkdir(mode=0o700)
        linked_root = self.state_root.parent / "linked-state"
        linked_root.symlink_to(target_root, target_is_directory=True)
        with self.assertRaises(DraftStoreError):
            DraftStore(linked_root).get("session-1")
        self.assertEqual(list(target_root.iterdir()), [])

        outside = self.state_root / "outside"
        with self.assertRaisesRegex(ValueError, "invalid session"):
            self.store.save("../escape", revision=None, enabled=True, text="private")
        outside.mkdir()
        hostile_root = self.state_root / "drafts"
        hostile_root.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(DraftStoreError):
            self.store.get("session-1")
        self.assertEqual(list(outside.iterdir()), [])

        hostile_root.unlink()
        self.store.save(
            "session-1",
            revision=None,
            enabled=True,
            text="private",
            images=[_IMAGE],
        )
        payload = next(path for path in self.store.root.glob("[0-9a-f]*.json"))
        payload.chmod(0o644)
        with self.assertRaises(DraftStoreError):
            self.store.get("session-1")
        payload.chmod(0o600)
        unknown = self.store.root / "unowned.txt"
        unknown.write_text("do not delete", encoding="utf-8")
        unknown.chmod(0o600)
        with self.assertRaises(DraftCorruptionError):
            self.store.get("session-1")
        self.assertEqual(unknown.read_text(encoding="utf-8"), "do not delete")


class DraftHttpTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        settings = SimpleNamespace(
            allowed_hosts=("127.0.0.1",),
            state_root=root / "state",
            auth_mode="pairing",
            tailscale_user=None,
            save_drafts=True,
        )
        self.model = FleetModel()
        self.model.load_record(
            {
                "id": "session-1",
                "title": "Draft session",
                "project": {"name": "draft", "path": str(root)},
                "adapterKind": "rpc",
                "ompSessionId": "omp-draft",
                "generation": 1,
                "updatedAt": "2026-09-19T00:00:00+00:00",
            }
        )
        for target, value in (
            ("SETTINGS", settings),
            ("AUTH_POLICY", AuthPolicy()),
            ("_fleet_model", self.model),
            ("_auth_manager", None),
        ):
            patcher = patch.object(server, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)
        self.host = f"127.0.0.1:{self.httpd.server_port}"
        self.origin = f"http://{self.host}"
        self.cookie, self.csrf = self._pair()

    def _stop_server(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(2)

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=60)
        payload = None if body is None else json.dumps(body, separators=(",", ":"))
        request_headers = {"Host": self.host, **(headers or {})}
        if payload is not None:
            request_headers["Content-Type"] = "application/json"
        connection.request(method, path, body=payload, headers=request_headers)
        response = connection.getresponse()
        raw = response.read()
        result = response.status, dict(response.headers), json.loads(raw) if raw else None
        connection.close()
        return result

    def _pair(self):
        secret = server.auth_manager().begin_pairing()
        status, headers, payload = self.request(
            "POST",
            "/api/auth/pair",
            {"token": secret.token},
            {"Origin": self.origin, "Sec-Fetch-Site": "same-origin"},
        )
        self.assertEqual(status, 200)
        return headers["Set-Cookie"].split(";", 1)[0], payload["csrfToken"]

    def get_headers(self):
        return {"Cookie": self.cookie}

    def post_headers(self, **changes):
        headers = {
            "Cookie": self.cookie,
            "Origin": self.origin,
            "Sec-Fetch-Site": "same-origin",
            "X-OMP-Deck-CSRF": self.csrf,
        }
        headers.update(changes)
        return headers

    def post(self, session_id, body, headers=None):
        return self.request(
            "POST",
            f"/api/sessions/{session_id}/draft",
            body,
            self.post_headers() if headers is None else headers,
        )

    def test_public_boundary_enforces_auth_origin_csrf_and_known_sessions(self):
        route = "/api/sessions/session-1/draft"
        status, headers, payload = self.request("GET", route)
        self.assertEqual((status, payload), (401, {"error": "authentication required"}))
        self.assertEqual(headers["Cache-Control"], "no-store")
        status, headers, payload = self.request("GET", route, headers=self.get_headers())
        self.assertEqual(status, 200)
        self.assertTrue(payload["enabled"])
        self.assertIsNone(payload["revision"])
        self.assertEqual(headers["Cache-Control"], "no-store")

        body = {"revision": None, "text": "private", "images": []}
        self.assertEqual(self.post("session-1", body, self.get_headers())[0], 403)
        self.assertEqual(
            self.post("session-1", body, self.post_headers(Origin="https://attacker.example"))[0],
            403,
        )
        self.assertEqual(self.post("missing", body)[0], 404)
        self.assertEqual(self.request("GET", "/api/sessions/missing/draft", headers=self.get_headers())[0], 404)

    def test_fleet_preflight_allows_drafts_only_for_the_enrolled_origin(self):
        origin = "https://deck.example"
        trust = SimpleNamespace(load=lambda: {"machineOrigin": self.origin, "coordinatorOrigin": origin})
        route = "/api/sessions/session-1/draft"
        with patch.object(server, "fleet_trust", return_value=trust):
            for method in ("GET", "POST"):
                headers = {
                    "Origin": origin,
                    "Access-Control-Request-Method": method,
                    "Access-Control-Request-Headers": "Authorization, Content-Type",
                }
                status, response_headers, _ = self.request("OPTIONS", route, headers=headers)
                self.assertEqual(status, 204)
                self.assertEqual(response_headers["Access-Control-Allow-Origin"], origin)
                self.assertEqual(response_headers["Access-Control-Allow-Methods"], method)
            self.assertEqual(
                self.request("OPTIONS", route, headers={**headers, "Origin": "https://attacker.example"})[0],
                403,
            )
            self.assertEqual(
                self.request("OPTIONS", route, headers={**headers, "Access-Control-Request-Method": "DELETE"})[0],
                403,
            )

    def test_http_cas_optional_images_and_effective_policy(self):
        first_body = {"revision": None, "text": "first", "images": [_IMAGE]}
        status, headers, first = self.post("session-1", first_body)
        self.assertEqual((status, headers["Cache-Control"]), (200, "no-store"))
        self.assertTrue(first["enabled"])
        self.assertNotIn("images", first)
        anonymous_status, _, anonymous = self.request("GET", "/api/sessions/session-1/draft")
        self.assertEqual(anonymous_status, 401)
        self.assertNotIn(_IMAGE["data"], json.dumps(anonymous))

        status, _, second = self.post(
            "session-1",
            {"revision": first["revision"], "text": "second"},
        )
        self.assertEqual(status, 200)
        status, _, loaded = self.request("GET", "/api/sessions/session-1/draft", headers=self.get_headers())
        self.assertEqual((loaded["text"], loaded["images"]), ("second", [_IMAGE]))

        status, _, conflict = self.post(
            "session-1",
            {"revision": first["revision"], "text": "stale"},
        )
        self.assertEqual(status, 409)
        self.assertEqual(conflict["draft"], loaded)

        _draft_http_store = server._draft_http._store(server.SETTINGS.state_root)
        _draft_http_store.save(
            "session-1",
            revision=second["revision"],
            enabled=False,
            text="",
            images=[],
        )
        projected = self.request("GET", "/api/sessions/session-1/draft", headers=self.get_headers())[2]
        self.assertTrue(projected["enabled"])
        self.assertEqual(projected["text"], "")
        status, _, _ = self.post(
            "session-1",
            {"revision": projected["revision"], "text": "delete this private draft", "images": [_IMAGE]},
        )
        self.assertEqual(status, 200)

        server.SETTINGS.save_drafts = False
        status, _, disabled = self.request("GET", "/api/sessions/session-1/draft", headers=self.get_headers())
        self.assertEqual((status, disabled["enabled"], disabled["revision"]), (200, False, None))
        erased = _draft_http_store.get("session-1")
        self.assertEqual((erased["revision"], erased["text"], erased["images"]), (None, "", []))
        status, _, _ = self.post("session-1", {"revision": None, "text": "malicious"})
        self.assertEqual(status, 403)

    def test_corrupt_persistence_returns_an_explicit_unavailable_response(self):
        status, _, _ = self.post(
            "session-1",
            {"revision": None, "text": "private", "images": []},
        )
        self.assertEqual(status, 200)
        (server.SETTINGS.state_root / "drafts/manifest.json").write_text("{", encoding="utf-8")

        status, _, _ = self.request(
            "GET",
            "/api/sessions/session-1/draft",
            headers=self.get_headers(),
        )
        self.assertEqual(status, 503)

    def test_malformed_and_oversize_images_are_rejected_at_http_boundary(self):
        bodies = (
            [{"data": "not-base64", "mimeType": "image/png"}],
            [{"data": _IMAGE["data"], "mimeType": "image/jpeg"}],
            [{**_IMAGE, "unexpected": True}],
            [_IMAGE] * 5,
        )
        for images in bodies:
            with self.subTest(images=len(images)):
                status, _, payload = self.post(
                    "session-1",
                    {"revision": None, "text": "", "images": images},
                )
                self.assertEqual(status, 400)
                self.assertIn("error", payload)

        oversized = b"\x89PNG\r\n\x1a\n" + (b"x" * (MAX_IMAGE_BYTES + 1 - 8))
        status, _, payload = self.post(
            "session-1",
            {
                "revision": None,
                "text": "",
                "images": [
                    {
                        "data": base64.b64encode(oversized).decode("ascii"),
                        "mimeType": "image/png",
                    }
                ],
            },
        )
        self.assertEqual(status, 400)
        self.assertIn("20 MiB", payload["error"])


if __name__ == "__main__":
    unittest.main()
