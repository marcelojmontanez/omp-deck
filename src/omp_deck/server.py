#!/usr/bin/env python3
import json
import re
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer as _ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from .auth import (
    COOKIE_NAME,
    AuthError,
    AuthManager,
    AuthenticationFailed,
    PairingFailed,
    PairingRateLimited,
)
from .attachments import detect_image_mime, read_blob
from .projects import create_project_directory, list_launchable_projects, resolve_project_path
from .fleet_model import FleetModel
from .rpc_sessions import (
    RpcSessionManager,
    SessionCommandConflictError,
    SessionNotConnectedError,
)
from .terminal_sessions import (
    TerminalCommandConflict,
    TerminalSessionManager,
    TerminalSessionNotConnected,
)
from .event_stream import ReplayUnavailableError
from .normalized_actions import NormalizedActionDispatcher
from .request_parsing import (
    cookie_credential,
    read_json_request,
    request_host as parse_request_host,
    trusted_browser_request as parse_trusted_browser_request,
)
from .read_cursors import CursorStore
from .settings import load_settings
from .usage import UsageService

ROOT = Path(__file__).parent
SETTINGS = load_settings()
HOST = SETTINGS.host
PORT = SETTINGS.port
SESSION_ROOT = SETTINGS.session_root
DEV_ROOT = SETTINGS.project_root
_auth_manager = None
_auth_lock = threading.Lock()
_cursor_store = CursorStore(SETTINGS.cursor_path)
_fleet_model = FleetModel(cursor_store=_cursor_store)
_usage_service = UsageService(SETTINGS.omp_bin)
_rpc_manager = None
_rpc_manager_lock = threading.Lock()
_terminal_manager = None


class ThreadingHTTPServer(_ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
_terminal_manager_lock = threading.Lock()


def rpc_manager():
    global _rpc_manager
    with _rpc_manager_lock:
        if _rpc_manager is None:
            _rpc_manager = RpcSessionManager(SETTINGS, _fleet_model)
        return _rpc_manager


def terminal_manager():
    global _terminal_manager
    with _terminal_manager_lock:
        if _terminal_manager is None:
            _terminal_manager = TerminalSessionManager(SETTINGS, _fleet_model)
        return _terminal_manager


def start_runtime():
    auth_manager()
    failures = dict(rpc_manager().recover_desired())
    failures.update(terminal_manager().start())
    return failures


def stop_runtime():
    with _terminal_manager_lock:
        terminal = _terminal_manager
    if terminal is not None:
        terminal.close()
    with _rpc_manager_lock:
        manager = _rpc_manager
    if manager is not None:
        manager.close()


def launch_rpc_project(project, request_id, create=False, title=None):
    if not isinstance(project, str):
        raise ValueError("invalid project")
    project = project.strip()
    path = project_path(project, must_exist=not create)
    created = False
    if create:
        path = create_project_directory(DEV_ROOT, project)
        created = True
    elif project not in launchable_projects():
        raise ValueError("invalid project")
    result = rpc_manager().launch_request(request_id, project, path, title=title)
    if result["status"] == "failed" and created:
        path.rmdir()
    return {**result, "created": created and result["status"] == "accepted"}


def dispatch_session_command(session_id, request_id, action, payload):
    session = _fleet_model.get(session_id)
    if session["transport"]["kind"] == "rpc":
        return rpc_manager().dispatch(session_id, request_id, action, payload)
    return terminal_manager().command(session_id, request_id, action, payload)


def project_path(project, *, must_exist=True):
    return resolve_project_path(DEV_ROOT, project, must_exist=must_exist)


def launchable_projects():
    return list_launchable_projects(DEV_ROOT)



def auth_manager():
    global _auth_manager
    if _auth_manager is None:
        with _auth_lock:
            if _auth_manager is None:
                _auth_manager = AuthManager(SETTINGS.state_root)
    return _auth_manager
def request_host(headers):
    return parse_request_host(headers, SETTINGS.allowed_hosts)


def trusted_browser_request(headers):
    return parse_trusted_browser_request(headers, SETTINGS.allowed_hosts)


def action_dispatcher():
    return NormalizedActionDispatcher(
        fleet_model=_fleet_model,
        session_root=SESSION_ROOT,
        launch_project=launch_rpc_project,
        dispatch_command=dispatch_session_command,
        terminal_manager=terminal_manager,
    )







class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT / "static"), **kwargs)
    def read_json(self, max_bytes=64 * 1024):
        return read_json_request(self, max_bytes)


    def log_message(self, format, *args):
        print(f"{self.client_address[0]} {format % args}")

    def end_headers(self):
        request_path = urlparse(self.path).path
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; base-uri 'none'; connect-src 'self'; "
            "font-src 'self'; form-action 'self'; frame-ancestors 'none'; "
            "img-src 'self' blob: data:; object-src 'none'; script-src 'self'; style-src 'self'",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Permissions-Policy",
            "camera=(), geolocation=(), microphone=(), payment=(), usb=()",
        )
        self.send_header(
            "Cache-Control",
            "no-store" if request_path.startswith("/api/") else "no-cache",
        )
        super().end_headers()

    def reply(self, status, payload, *, headers=()):
        body = json.dumps(payload).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            for name, value in headers:
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_sse(self):
        raw_cursor = self.headers.get("Last-Event-ID")
        if raw_cursor is None or raw_cursor == "":
            cursor = _fleet_model.journal.latest_id
            initial = [(
                cursor,
                {
                    "id": cursor,
                    "type": "snapshot",
                    "payload": _fleet_model.snapshot(),
                },
            )]
        elif not raw_cursor.isascii() or not raw_cursor.isdecimal() or len(raw_cursor) > 20:
            self.reply(400, {"error": "invalid Last-Event-ID"})
            return
        else:
            cursor = int(raw_cursor)
            if cursor > _fleet_model.journal.latest_id:
                self.reply(400, {"error": "invalid Last-Event-ID"})
                return
            try:
                initial = [(event.id, event.public()) for event in _fleet_model.journal.replay_after(cursor)]
            except ReplayUnavailableError:
                cursor = _fleet_model.journal.latest_id
                initial = [(
                    cursor,
                    {
                        "id": cursor,
                        "type": "snapshot",
                        "payload": _fleet_model.snapshot(),
                    },
                )]
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.connection.settimeout(20)
            for event_id, payload in initial:
                self._write_sse_event(event_id, payload)
                cursor = event_id
            while True:
                try:
                    events = _fleet_model.journal.wait_after(cursor, 15)
                except ReplayUnavailableError:
                    cursor = _fleet_model.journal.latest_id
                    self._write_sse_event(cursor, {
                        "id": cursor,
                        "type": "snapshot",
                        "payload": _fleet_model.snapshot(),
                    })
                    continue
                if not events:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                for event in events:
                    self._write_sse_event(event.id, event.public())
                    cursor = event.id
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            return

    def _write_sse_event(self, event_id, payload):
        data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        frame = f"id: {event_id}\nevent: fleet\ndata: {data}\n\n".encode("utf-8")
        self.wfile.write(frame)
        self.wfile.flush()

    def reply_blob(self, digest):
        blob = read_blob(digest)
        if blob is None:
            self.reply(404, {"error": "image not found"})
            return
        _, body = blob
        self.send_response(200)
        self.send_header("Content-Type", detect_image_mime(body) or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authenticate_request(self, *, csrf=False):
        credential = cookie_credential(self.headers)
        if csrf:
            return auth_manager().validate_csrf(
                credential, self.headers.get("X-OMP-Deck-CSRF")
            )
        return auth_manager().authenticate(credential)

    def do_GET(self):
        request_path = urlparse(self.path).path
        if request_host(self.headers) is None:
            self.reply(400, {"error": "invalid host"})
            return
        if request_path == "/healthz":
            try:
                auth_manager()
            except Exception:
                self.reply(503, {"ok": False, "configuration": True, "authentication": False})
                return
            self.reply(
                200,
                {
                    "ok": True,
                    "configuration": True,
                    "authentication": True,
                    "cursors": _cursor_store.healthy,
                },
            )
            return
        if request_path == "/api/auth/session":
            try:
                device = self.authenticate_request()
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
                )
            return
        if request_path.startswith("/api/"):
            try:
                self.authenticate_request()
            except AuthenticationFailed:
                self.reply(401, {"error": "authentication required"})
                return
            except AuthError:
                self.reply(503, {"error": "authentication state unavailable"})
                return
        if request_path.startswith("/api/blob/"):
            self.reply_blob(request_path.rsplit("/", 1)[-1])
            return
        if request_path == "/api/events":
            self.send_sse()
            return
        if request_path == "/api/sessions":
            try:
                rpc_manager().request_refresh_connected()
                self.reply(200, _fleet_model.snapshot())
            except Exception:
                self.reply(503, {"error": "session state unavailable"})
            return
        if request_path == "/api/usage":
            self.reply(200, _usage_service.snapshot())
            return
        if request_path == "/api/projects":
            self.reply(200, {"projects": launchable_projects(), "rootLabel": f"{DEV_ROOT.name}/"})
            return
        super().do_GET()
    def do_POST(self):
        request_path = urlparse(self.path).path
        if not trusted_browser_request(self.headers):
            self.reply(403, {"error": "request origin is not allowed"})
            return
        if request_path == "/api/auth/pair":
            self._pair()
            return
        credential = cookie_credential(self.headers)
        if not self._authorize_mutation():
            return
        data = self._read_post_body(request_path)
        if data is None:
            return
        if self._auth_action(request_path, credential):
            return
        self._normalized_action(request_path, data)

    def _pair(self):
        try:
            data = self.read_json(4096)
            paired = auth_manager().pair(data.get("token"), source=str(self.client_address[0]))
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
        self.reply(200, {
            "authenticated": True,
            "deviceId": paired.device.device_id,
            "csrfToken": paired.device.csrf_token,
        }, headers=(("Set-Cookie", paired.set_cookie),))

    def _authorize_mutation(self):
        try:
            self.authenticate_request(csrf=True)
            return True
        except AuthenticationFailed:
            self.reply(403, {"error": "authentication and CSRF token required"})
        except AuthError:
            self.reply(503, {"error": "authentication state unavailable"})
        return False

    def _read_post_body(self, request_path):
        limit = (
            2 * 1024 * 1024
            if re.fullmatch(r"/api/sessions/[^/]+/(messages|follow-ups)", request_path)
            else 64 * 1024
        )
        try:
            return self.read_json(limit)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            self.reply(400, {"error": str(error)})
        except TimeoutError:
            self.reply(408, {"error": "request body timed out"})
        return None

    def _auth_action(self, request_path, credential):
        if request_path == "/api/auth/logout":
            cookie = auth_manager().logout(credential)
        elif request_path == "/api/auth/revoke-all":
            cookie = auth_manager().revoke_all(
                credential, self.headers.get("X-OMP-Deck-CSRF")
            )
        else:
            return False
        self.reply(200, {"ok": True}, headers=(("Set-Cookie", cookie),))
        return True

    def _normalized_action(self, request_path, data):
        try:
            response = action_dispatcher().dispatch(request_path, data)
            if response is None:
                self.reply(404, {"error": "not found"})
                return
            self.reply(*response)
        except (SessionCommandConflictError, TerminalCommandConflict) as error:
            self.reply(409, {"error": str(error), "errorCode": "request_conflict"})
        except KeyError:
            self.reply(404, {"error": "session not found"})
        except (TypeError, ValueError) as error:
            message = "invalid Collab link" if request_path == "/api/terminal-sessions/collab" else str(error)
            self.reply(400, {"error": message})
        except (SessionNotConnectedError, TerminalSessionNotConnected):
            self.reply(409, {"error": "session is not connected"})
        except (OSError, RuntimeError) as error:
            message = (
                "Collaboration connection unavailable"
                if request_path == "/api/terminal-sessions/collab"
                else str(error)
            )
            self.reply(502, {"error": message})
        except Exception:
            if request_path == "/api/terminal-sessions/collab":
                self.reply(502, {"error": "Collaboration connection unavailable"})
                return
            raise

if __name__ == "__main__":
    from .cli import serve

    serve()
