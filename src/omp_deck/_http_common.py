from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import threading
from http.server import SimpleHTTPRequestHandler
from http.server import ThreadingHTTPServer as _ThreadingHTTPServer
from pathlib import Path
from socketserver import TCPServer

from .auth import COOKIE_NAME, AuthenticationFailed, AuthError, AuthManager, PairingFailed, PairingRateLimited
from .auth_policy import TAILSCALE_AUTH, AuthPolicy
from .request_parsing import cookie_credential, read_json_request, request_host, trusted_browser_request

_STATIC_ROOT = Path(__file__).parent / "static"


class ThreadingHTTPServer(_ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    max_connections = 64
    header_timeout = 10.0
    body_timeout = 30.0

    def __init__(self, server_address, handler, bind_and_activate=True):
        # A browser asset burst overflows TCPServer's backlog of 5, adding
        # measured one-second TCP retries despite spare request workers.
        self.request_queue_size = self.max_connections
        self._connections = threading.BoundedSemaphore(self.max_connections)
        if ":" in server_address[0]:
            self.address_family = socket.AF_INET6
        super().__init__(server_address, handler, bind_and_activate)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(self.header_timeout)
        return connection, address

    def process_request(self, request, client_address):
        # Admission never blocks accept(): slow unauthenticated peers cannot
        # allocate unbounded threads or keep the listener from rejecting excess.
        if not self._connections.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._connections.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._connections.release()

    def server_bind(self) -> None:
        # HTTPServer's reverse-DNS lookup can block before listen(). Host is
        # authorized at the HTTP boundary, so only the bound address is needed.
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class DeckHttpHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(_STATIC_ROOT), **kwargs)

    def handle_one_request(self):
        self._public_asset = False
        self._headers_complete = False
        # A socket idle timeout alone lets a trickling header hold a worker
        # forever. End this absolute deadline before dispatching SSE or actions.
        self._header_timer = threading.Timer(self.server.header_timeout, self._expire_headers)
        self._header_timer.daemon = True
        self._header_timer.start()
        try:
            super().handle_one_request()
        except ConnectionResetError:
            if self._headers_complete:
                raise
            # A suspended browser can reset an idle socket before sending a request.
            self.close_connection = True
        finally:
            self._header_timer.cancel()

    def parse_request(self):
        try:
            parsed = super().parse_request()
            self._headers_complete = parsed
            return parsed
        finally:
            self._header_timer.cancel()

    def _expire_headers(self):
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def send_head(self):
        # Only content identity can validate an unversioned asset across rollback.
        del self.headers["If-Modified-Since"]
        path = Path(self.translate_path(self.path))
        if path.parent != Path(self.directory) or path.suffix not in {".js", ".css"} or not path.is_file():
            return super().send_head()
        return self._send_public_asset(path)

    def _send_public_asset(self, path):
        try:
            source = path.open("rb")
        except OSError:
            self.send_error(404, "File not found")
            return None
        try:
            etag = f'"{hashlib.file_digest(source, "sha256").hexdigest()}"'
            candidates = self.headers.get("If-None-Match", "").split(",")
            unchanged = any(value.strip().removeprefix("W/") in {etag, "*"} for value in candidates)
            self._public_asset = True
            self.send_response(304 if unchanged else 200)
            self.send_header("ETag", etag)
            if unchanged:
                self.end_headers()
                source.close()
                return None
            self.send_header("Content-Type", self.guess_type(str(path)))
            self.send_header("Content-Length", str(os.fstat(source.fileno()).st_size))
            self.end_headers()
            source.seek(0)
            return source
        except BaseException:
            source.close()
            raise

    def auth_backend(self) -> AuthManager:
        raise NotImplementedError

    def allowed_hosts(self) -> tuple[str, ...]:
        raise NotImplementedError

    def cookie_name(self) -> str:
        return COOKIE_NAME

    def auth_policy(self) -> AuthPolicy:
        return AuthPolicy()

    def connect_origins(self) -> tuple[str, ...]:
        return ()

    def cors_origin(self) -> str | None:
        return None

    def request_authenticated(self, *, csrf: bool = False):
        self.auth_policy().require_request_identity(self.headers, self.client_address)
        credential = cookie_credential(self.headers, self.cookie_name())
        if csrf:
            return self.auth_backend().validate_csrf(credential, self.headers.get("X-OMP-Deck-CSRF"))
        return self.auth_backend().authenticate(credential)

    def read_json(self, max_bytes: int = 64 * 1024, *, timeout: float | None = None):
        return read_json_request(self, max_bytes, timeout=timeout)

    def reply(self, status: int, payload, *, headers=(), write_timeout: float | None = None) -> None:
        self.reply_bytes(
            status,
            json.dumps(payload).encode(),
            content_type="application/json",
            headers=headers,
            write_timeout=write_timeout,
        )

    def reply_bytes(self, status: int, body: bytes, *, content_type: str, headers=(), write_timeout=None) -> None:
        previous_timeout = self.connection.gettimeout() if write_timeout is not None else None
        try:
            if write_timeout is not None:
                self.connection.settimeout(write_timeout)
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            for name, value in headers:
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except TimeoutError:
            self.close_connection = True
        finally:
            if write_timeout is not None:
                try:
                    self.connection.settimeout(previous_timeout)
                except OSError:
                    pass

    def host_valid(self) -> bool:
        return request_host(self.headers, self.allowed_hosts()) is not None

    def browser_origin_valid(self) -> bool:
        return trusted_browser_request(self.headers, self.allowed_hosts())

    def handle_auth_get(self, path: str) -> bool:
        if path != "/api/auth/session":
            return False
        if not self.host_valid():
            self.reply(400, {"error": "invalid host"})
            return True
        try:
            device = self.request_authenticated()
        except AuthenticationFailed:
            self.reply(200, {"authenticated": False})
        except AuthError:
            self.reply(503, {"error": "authentication state unavailable"})
        else:
            self.reply(
                200,
                {
                    "authenticated": True,
                    "deviceId": device.device_id,
                    "csrfToken": device.csrf_token,
                },
                headers=(
                    (
                        "Set-Cookie",
                        self.auth_backend().cookie_header(
                            cookie_credential(self.headers, self.cookie_name()), device.expires_at
                        ),
                    ),
                ),
            )
        return True

    def handle_auth_post(self, path: str) -> bool:
        if path not in {"/api/auth/pair", "/api/auth/tailscale", "/api/auth/logout", "/api/auth/revoke-all"}:
            return False
        if not self.host_valid():
            self.reply(400, {"error": "invalid host"})
            return True
        if not self.browser_origin_valid():
            self.reply(403, {"error": "request origin is not allowed"})
            return True
        if path == "/api/auth/pair":
            if not self.auth_policy().pairing_enabled:
                self.reply(403, {"error": "pairing is disabled"})
            else:
                self._pair()
            return True
        if path == "/api/auth/tailscale":
            self._tailscale_login()
            return True
        try:
            self.request_authenticated(csrf=True)
            self.read_json()
            credential = cookie_credential(self.headers, self.cookie_name())
            if path == "/api/auth/logout":
                cookie = self.auth_backend().logout(credential)
            else:
                cookie = self.auth_backend().revoke_all(credential, self.headers.get("X-OMP-Deck-CSRF"))
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            self.reply(400, {"error": str(error)})
        except TimeoutError:
            self.reply(408, {"error": "request body timed out"})
        except AuthenticationFailed:
            self.reply(403, {"error": "authentication and CSRF token required"})
        except AuthError:
            self.reply(503, {"error": "authentication state unavailable"})
        else:
            self.reply(200, {"ok": True}, headers=(("Set-Cookie", cookie),))
        return True

    def _pair(self) -> None:
        try:
            data = self.read_json(4096)
            paired = self.auth_backend().pair(data.get("token"), source=str(self.client_address[0]))
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            self.reply(400, {"error": str(error)})
            return
        except TimeoutError:
            self.reply(408, {"error": "request body timed out"})
            return
        except PairingRateLimited:
            self.reply(429, {"error": "pairing temporarily unavailable"})
            return
        except PairingFailed:
            self.reply(401, {"error": "invalid or expired pairing code"})
            return
        except AuthError:
            self.reply(503, {"error": "authentication state unavailable"})
            return
        self.reply(
            200,
            {
                "authenticated": True,
                "deviceId": paired.device.device_id,
                "csrfToken": paired.device.csrf_token,
            },
            headers=(("Set-Cookie", paired.set_cookie),),
        )

    def _tailscale_login(self) -> None:
        policy = self.auth_policy()
        if policy.mode != TAILSCALE_AUTH:
            self.reply(403, {"error": "Tailscale authentication is disabled"})
            return
        try:
            policy.require_request_identity(self.headers, self.client_address)
            data = self.read_json(4096)
            if data:
                raise ValueError("request body must be empty")
            credential = cookie_credential(self.headers, self.cookie_name())
            issued = self.auth_backend().authenticate_or_issue(credential)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            self.reply(400, {"error": str(error)})
            return
        except TimeoutError:
            self.reply(408, {"error": "request body timed out"})
            return
        except AuthenticationFailed:
            self.reply(403, {"error": "Tailscale authentication denied"})
            return
        except AuthError:
            self.reply(503, {"error": "authentication state unavailable"})
            return
        self.reply(
            200,
            {
                "authenticated": True,
                "deviceId": issued.device.device_id,
                "csrfToken": issued.device.csrf_token,
            },
            headers=(("Set-Cookie", issued.set_cookie),),
        )

    def log_message(self, format, *args) -> None:
        # Generic HTTP errors can contain attacker-controlled request contents.
        pass

    def log_request(self, code="-", size="-") -> None:
        method = self.command if isinstance(self.command, str) and re.fullmatch(r"[A-Z]{1,16}", self.command) else "-"
        status = str(code) if re.fullmatch(r"[1-5][0-9]{2}", str(code)) else "-"
        print(f"{method} {status}", flush=True)

    def end_headers(self) -> None:
        connect_sources = " ".join(("'self'", *self.connect_origins()))
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; base-uri 'none'; "
            f"connect-src {connect_sources}; "
            "font-src 'self'; form-action 'self'; frame-ancestors 'none'; "
            "img-src 'self' blob: data:; object-src 'none'; script-src 'self'; style-src 'self'",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Permissions-Policy",
            "camera=(), geolocation=(), microphone=(), payment=(), usb=()",
        )
        self.send_header("Cache-Control", "public, no-cache" if getattr(self, "_public_asset", False) else "no-store")
        origin = self.cors_origin()
        if origin is not None:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        super().end_headers()
