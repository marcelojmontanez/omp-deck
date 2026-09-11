import contextlib
import http.client
import queue
import socket
import threading
import time
import unittest

from omp_deck._http_common import DeckHttpHandler, ThreadingHTTPServer


class HttpLimitTests(unittest.TestCase):
    @contextlib.contextmanager
    def running_server(self, *, limit=2, deadline=0.2, response_delay=0, hold=None):
        started = queue.Queue()

        class Handler(DeckHttpHandler):
            def setup(self):
                super().setup()
                started.put(True)

            def do_GET(self):
                if self.path == "/held" and hold is not None:
                    started.put("held")
                    hold.wait(5)
                time.sleep(response_delay)
                self.reply(200, {"ok": True})

        class LimitedServer(ThreadingHTTPServer):
            max_connections = limit
            header_timeout = deadline

        server = LimitedServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server.server_address, started
        finally:
            if hold is not None:
                hold.set()
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_partial_headers_expire_even_while_bytes_keep_arriving(self):
        with self.running_server() as (address, started), socket.create_connection(address, timeout=2) as client:
            started.get(timeout=2)
            client.sendall(b"GET / HTTP/1.1\r\nX-Audit: ")
            stop = threading.Event()

            def trickle():
                while not stop.wait(0.03):
                    try:
                        client.sendall(b"x")
                    except OSError:
                        return

            sender = threading.Thread(target=trickle)
            sender.start()
            try:
                client.settimeout(1)
                try:
                    received = client.recv(4096)
                except ConnectionResetError:
                    received = b""
                self.assertEqual(received, b"")
            finally:
                stop.set()
                sender.join(2)

    def test_capacity_rejects_excess_connection_then_recovers(self):
        release = threading.Event()
        with self.running_server(deadline=2, hold=release) as (address, started), contextlib.ExitStack() as stack:
            for _ in range(2):
                client = stack.enter_context(socket.create_connection(address, timeout=2))
                client.sendall(b"GET /held HTTP/1.1\r\nHost: localhost\r\n\r\n")
                self.assertIs(started.get(timeout=2), True)
                self.assertEqual(started.get(timeout=2), "held")
            with socket.create_connection(address, timeout=2) as excess:
                excess.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
                try:
                    received = excess.recv(4096)
                except ConnectionResetError:
                    received = b""
                self.assertEqual(received, b"")
            release.set()
            # Completed handlers release slots independently of header expiry.
            until = time.monotonic() + 2
            while True:
                connection = http.client.HTTPConnection(*address, timeout=1)
                try:
                    connection.request("GET", "/")
                    response = connection.getresponse()
                    self.assertEqual((response.status, response.read()), (200, b'{"ok": true}'))
                    break
                except (ConnectionError, http.client.RemoteDisconnected):
                    if time.monotonic() >= until:
                        raise
                    time.sleep(0.02)
                finally:
                    connection.close()

    def test_header_deadline_does_not_expire_completed_request(self):
        with self.running_server(response_delay=0.4) as (address, _):
            connection = http.client.HTTPConnection(*address, timeout=2)
            try:
                connection.request("GET", "/")
                response = connection.getresponse()
                self.assertEqual((response.status, response.read()), (200, b'{"ok": true}'))
            finally:
                connection.close()
