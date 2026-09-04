import http.client
import json
import socket
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from omp_deck import server
from omp_deck.auth import AuthenticationFailed
from omp_deck.event_stream import EventJournal


class _AuthManager:
    credential = "test-device-credential"
    revoked = False

    def authenticate(self, credential):
        if credential != self.credential or self.revoked:
            raise AuthenticationFailed("authentication required")
        return SimpleNamespace(device_id="test-device", csrf_token="test-csrf")


class _FleetModel:
    def __init__(self, *, max_events=2_000):
        self.journal = EventJournal(max_events=max_events)
        self.snapshot_value = {"sessions": [{"id": "authoritative"}]}

    def snapshot(self):
        return {
            **self.snapshot_value,
            "lastEventId": self.journal.latest_id,
        }


class ServerSseBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.auth = _AuthManager()
        self.model = _FleetModel()
        self._model_patch = patch.object(server, "_fleet_model", self.model)
        self._auth_patch = patch.object(server, "_auth_manager", self.auth)
        self._model_patch.start()
        self._auth_patch.start()
        self.addCleanup(self._auth_patch.stop)
        self.addCleanup(self._model_patch.stop)

        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = self.httpd.server_address[1]
        self.server_thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.server_thread.start()
        self.connections = []
        self.addCleanup(self._stop_server)

    def _stop_server(self):
        for connection in self.connections:
            connection.close()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.server_thread.join(2)
        self.assertFalse(self.server_thread.is_alive())

    def _request(self, *, cursor=None, authenticated=True):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=1)
        self.connections.append(connection)
        headers = {}
        if authenticated:
            headers["Cookie"] = f"{server.COOKIE_NAME}={self.auth.credential}"
        if cursor is not None:
            headers["Last-Event-ID"] = str(cursor)
        connection.request("GET", "/api/events", headers=headers)
        return connection, connection.getresponse()

    def _read_frame(self, response):
        fields = {}
        while True:
            line = response.fp.readline()
            if not line:
                self.fail("SSE connection closed before a complete frame")
            if line == b"\n":
                if fields:
                    return fields
                continue
            if line.startswith(b":"):
                continue
            name, separator, value = line.rstrip(b"\r\n").partition(b":")
            self.assertTrue(separator, line)
            fields[name.decode("ascii")] = value.lstrip().decode("utf-8")

    def _publish(self, number):
        return self.model.journal.publish(
            snapshot_generation=number,
            event_type="change",
            payload={"number": number},
        )

    def _cursor(self, event_id, *, stream_id=None):
        identity = stream_id or self.model.journal.stream_id
        return f"{identity}:{event_id}"

    def test_unauthenticated_events_request_is_401(self):
        _, response = self._request(authenticated=False)
        self.assertEqual(response.status, 401)
        self.assertEqual(json.loads(response.read()), {"error": "authentication required"})

    def test_initial_stream_has_sse_headers_and_current_snapshot_id(self):
        self._publish(1)
        current = self._publish(2).id

        _, response = self._request()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Cache-Control"), "no-store")
        self.assertEqual(response.getheader("Content-Type"), "text/event-stream; charset=utf-8")
        frame = self._read_frame(response)

        self.assertEqual(frame["id"], self._cursor(current))
        with self.assertRaises(socket.timeout):
            response.fp.readline()
        self.assertEqual(frame["event"], "fleet")
        self.assertEqual(
            json.loads(frame["data"]),
            {"id": current, "type": "snapshot", "payload": self.model.snapshot()},
        )

    def test_last_event_id_replays_ordered_missed_events_without_snapshot(self):
        first = self._publish(1)
        second = self._publish(2)
        third = self._publish(3)

        _, response = self._request(cursor=self._cursor(first.id))
        frames = [self._read_frame(response), self._read_frame(response)]
        payloads = [json.loads(frame["data"]) for frame in frames]

        self.assertEqual(
            [frame["id"] for frame in frames],
            [self._cursor(second.id), self._cursor(third.id)],
        )
        self.assertEqual([payload["type"] for payload in payloads], ["change", "change"])
        self.assertNotIn("snapshot", [payload["type"] for payload in payloads])

    def test_stale_cursor_receives_authoritative_replacement_snapshot(self):
        self.model = _FleetModel(max_events=2)
        server._fleet_model = self.model
        self._publish(1)
        self._publish(2)
        current = self._publish(3).id

        _, response = self._request(cursor=self._cursor(0))
        frame = self._read_frame(response)

        self.assertEqual(frame["id"], self._cursor(current))
        self.assertEqual(
            json.loads(frame["data"]),
            {"id": current, "type": "snapshot", "payload": self.model.snapshot()},
        )

    def test_invalid_cursor_is_rejected_without_hanging(self):
        self._publish(1)
        started = time.monotonic()
        _, response = self._request(cursor="not-a-number")
        self.assertEqual(response.status, 400)
        self.assertEqual(json.loads(response.read()), {"error": "invalid Last-Event-ID"})
        self.assertLess(time.monotonic() - started, 1)

    def test_future_cursor_from_previous_server_epoch_receives_snapshot(self):
        current = self._publish(1).id

        _, response = self._request(cursor=self._cursor(current + 20))
        frame = self._read_frame(response)

        self.assertEqual(response.status, 200)
        self.assertEqual(frame["id"], self._cursor(current))
        self.assertEqual(
            json.loads(frame["data"]),
            {"id": current, "type": "snapshot", "payload": self.model.snapshot()},
        )

    def test_event_published_after_connection_arrives_live(self):
        _, response = self._request()
        snapshot = self._read_frame(response)
        self.assertEqual(snapshot["id"], self._cursor(0))

        published = self._publish(1)
        live = self._read_frame(response)

        self.assertEqual(live["id"], self._cursor(published.id))
        self.assertEqual(json.loads(live["data"])["payload"], {"number": 1})

    def test_previous_server_epoch_receives_authoritative_snapshot(self):
        current = self._publish(1).id

        _, response = self._request(
            cursor=self._cursor(0, stream_id="0" * 32),
        )
        frame = self._read_frame(response)

        self.assertEqual(frame["id"], self._cursor(current))
        self.assertEqual(json.loads(frame["data"])["type"], "snapshot")

    def test_snapshot_cursor_matches_changes_published_during_snapshot(self):
        published = False

        def racing_snapshot():
            nonlocal published
            if not published:
                published = True
                self._publish(1)
            return {
                **self.model.snapshot_value,
                "lastEventId": self.model.journal.latest_id,
            }

        with patch.object(self.model, "snapshot", side_effect=racing_snapshot):
            _, response = self._request()
            frame = self._read_frame(response)

        self.assertEqual(frame["id"], self._cursor(1))
        self.assertEqual(json.loads(frame["data"])["payload"]["lastEventId"], 1)

    def test_revoked_live_stream_closes_before_delivering_more_events(self):
        _, response = self._request()
        self._read_frame(response)
        self.auth.revoked = True

        self._publish(1)

        self.assertEqual(response.fp.readline(), b"")

    def test_legacy_numeric_cursor_receives_authoritative_snapshot(self):
        current = self._publish(1).id

        _, response = self._request(cursor=0)
        frame = self._read_frame(response)

        self.assertEqual(frame["id"], self._cursor(current))
        self.assertEqual(json.loads(frame["data"])["type"], "snapshot")

    def test_disconnected_stream_does_not_block_server_close(self):
        connection, response = self._request()
        self._read_frame(response)
        connection.close()

        started = time.monotonic()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.server_thread.join(1)

        self.assertFalse(self.server_thread.is_alive())
        self.assertLess(time.monotonic() - started, 1)


if __name__ == "__main__":
    unittest.main()
