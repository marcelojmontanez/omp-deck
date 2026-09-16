#!/usr/bin/env python3
import json
import re
import threading
from urllib.parse import urlparse, urlsplit

from ._fleet_trust import FleetTrustStore
from ._http_common import DeckHttpHandler
from ._http_common import ThreadingHTTPServer as ThreadingHTTPServer
from ._rpc_dispatch import SessionCommandConflictError, SessionNotConnectedError
from ._sse import send_stream
from ._terminal_commands import TerminalCommandConflict
from .attachments import detect_image_mime, read_blob
from .auth import COOKIE_NAME as COOKIE_NAME
from .auth import AuthenticationFailed, AuthError, AuthManager
from .auth_policy import AuthPolicy
from .fleet_model import FleetModel
from .fleet_protocol import verify_ticket
from .normalized_actions import NormalizedActionDispatcher
from .projects import list_launchable_projects, resolve_project_path
from .read_cursors import CursorStore
from .rpc_sessions import RpcSessionManager
from .settings import load_settings
from .terminal_sessions import (
    TerminalSessionManager,
    TerminalSessionNotConnected,
)
from .usage import UsageService

SETTINGS = load_settings()
HOST = SETTINGS.host
PORT = SETTINGS.port
SESSION_ROOT = SETTINGS.session_root
DEV_ROOT = SETTINGS.project_root
AUTH_POLICY = AuthPolicy(SETTINGS.auth_mode, SETTINGS.tailscale_user)
_auth_manager = None
_auth_lock = threading.Lock()
_cursor_store = None
_cursor_store_lock = threading.Lock()
_fleet_trust_store = None
_fleet_trust_lock = threading.Lock()
_fleet_model = FleetModel()
_usage_service = UsageService(SETTINGS.omp_bin)
_rpc_manager = None
_rpc_manager_lock = threading.Lock()
_terminal_manager = None


_terminal_manager_lock = threading.Lock()


def cursor_store():
    global _cursor_store
    auth_manager()
    with _cursor_store_lock:
        if _cursor_store is None:
            _cursor_store = CursorStore(SETTINGS.cursor_path)
            _fleet_model.cursor_store = _cursor_store
        return _cursor_store


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
    cursor_store()
    failures = dict(rpc_manager().recover_desired())
    failures.update(terminal_manager().start())
    return failures


def stop_runtime():
    errors = []
    with _terminal_manager_lock:
        terminal = _terminal_manager
    if terminal is not None:
        try:
            terminal.close()
        except Exception as error:
            errors.append(("terminal", error))
    with _rpc_manager_lock:
        manager = _rpc_manager
    if manager is not None:
        try:
            manager.close()
        except Exception as error:
            errors.append(("RPC", error))
    if errors:
        detail = "; ".join(f"{label}: {error}" for label, error in errors)
        raise RuntimeError(f"runtime shutdown failed: {detail}") from errors[0][1]


def launch_rpc_project(project, request_id, create=False, title=None):
    if not isinstance(project, str):
        raise ValueError("invalid project")
    if not create and project not in launchable_projects():
        raise ValueError("invalid project")
    return rpc_manager().launch_request(
        request_id,
        project,
        title=title,
        create=create,
    )


def dispatch_session_command(session_id, request_id, action, payload):
    session = _fleet_model.get(session_id)
    if session["transport"]["kind"] == "rpc":
        return rpc_manager().dispatch(session_id, request_id, action, payload)
    return terminal_manager().command(session_id, request_id, action, payload)


def project_path(project, *, must_exist=True):
    return resolve_project_path(DEV_ROOT, project, must_exist=must_exist)


def launchable_projects():
    return list_launchable_projects(DEV_ROOT, max_depth=SETTINGS.project_discovery_depth)


def auth_manager():
    global _auth_manager
    if _auth_manager is None:
        with _auth_lock:
            if _auth_manager is None:
                _auth_manager = AuthManager(SETTINGS.state_root)
    return _auth_manager


def fleet_trust():
    global _fleet_trust_store
    expected_path = SETTINGS.state_root / "fleet-node.json"
    if _fleet_trust_store is None or _fleet_trust_store.path != expected_path:
        with _fleet_trust_lock:
            if _fleet_trust_store is None or _fleet_trust_store.path != expected_path:
                _fleet_trust_store = FleetTrustStore(SETTINGS.state_root)
    return _fleet_trust_store


def action_dispatcher():
    return NormalizedActionDispatcher(
        fleet_model=_fleet_model,
        session_root=SESSION_ROOT,
        launch_project=launch_rpc_project,
        dispatch_command=dispatch_session_command,
        terminal_manager=terminal_manager,
    )


class Handler(DeckHttpHandler):
    def auth_backend(self):
        return auth_manager()

    def auth_policy(self):
        return AUTH_POLICY

    def allowed_hosts(self):
        return SETTINGS.allowed_hosts

    def cors_origin(self):
        try:
            enrollment = fleet_trust().load()
        except (OSError, RuntimeError):
            return None
        if enrollment is not None and self._fleet_request_boundary(enrollment):
            return enrollment["coordinatorOrigin"]
        return None

    def request_authenticated(self, *, csrf=False):
        authorization = self.headers.get_all("Authorization", [])
        if authorization:
            if len(authorization) != 1:
                raise AuthenticationFailed("invalid fleet authorization")
            value = authorization[0]
            if not isinstance(value, str) or not value.startswith("Bearer ") or value.count(" ") != 1:
                raise AuthenticationFailed("invalid fleet authorization")
            token = value[7:]
            if not token or len(token.encode("utf-8")) > 4096:
                raise AuthenticationFailed("invalid fleet authorization")
            try:
                enrollment = fleet_trust().load()
            except (OSError, RuntimeError) as error:
                raise AuthError("fleet trust state unavailable") from error
            if enrollment is None or not self._fleet_request_boundary(enrollment):
                raise AuthenticationFailed("fleet request boundary rejected")
            return verify_ticket(token, enrollment)
        return super().request_authenticated(csrf=csrf)

    def _fleet_request_boundary(self, enrollment):
        hosts = self.headers.get_all("Host", [])
        origins = self.headers.get_all("Origin", [])
        return (
            len(hosts) == 1
            and len(origins) == 1
            and self.host_valid()
            and hosts[0] == urlsplit(enrollment["machineOrigin"]).netloc
            and origins[0] == enrollment["coordinatorOrigin"]
        )

    def _mutation_origin_valid(self):
        if self.headers.get_all("Authorization", []):
            try:
                enrollment = fleet_trust().load()
            except (OSError, RuntimeError):
                return False
            return enrollment is not None and self._fleet_request_boundary(enrollment)
        return self.browser_origin_valid()

    @staticmethod
    def _preflight_route_known(path, method):
        if method == "GET":
            return (
                path
                in {
                    "/api/client-config",
                    "/api/events",
                    "/api/projects",
                    "/api/sessions",
                    "/api/usage",
                }
                or re.fullmatch(r"/api/blob/[^/]+", path) is not None
            )
        if method != "POST":
            return False
        return (
            path == "/api/terminal-sessions/collab"
            or re.fullmatch(r"/api/projects/.+/sessions", path) is not None
            or re.fullmatch(
                r"/api/sessions/[^/]+/(messages|follow-ups|interrupt|shutdown|model|thinking|branch|switch|resume|compact|read|remove)",
                path,
            )
            is not None
            or re.fullmatch(r"/api/sessions/[^/]+/ui-requests/[^/]+/(respond|cancel)", path) is not None
        )

    def do_OPTIONS(self):
        request_path = urlparse(self.path).path
        methods = self.headers.get_all("Access-Control-Request-Method", [])
        requested_header_values = self.headers.get_all("Access-Control-Request-Headers", [])
        private_network_values = self.headers.get_all("Access-Control-Request-Private-Network", [])
        requested_method = methods[0] if len(methods) == 1 else ""
        requested_headers = requested_header_values[0] if len(requested_header_values) == 1 else ""
        private_network = private_network_values[0] if len(private_network_values) == 1 else None
        try:
            enrollment = fleet_trust().load()
        except (OSError, RuntimeError):
            enrollment = None
        header_names = {item.strip().lower() for item in requested_headers.split(",") if item.strip()}
        if (
            enrollment is None
            or not self._fleet_request_boundary(enrollment)
            or not self._preflight_route_known(request_path, requested_method)
            or len(requested_header_values) > 1
            or len(private_network_values) > 1
            or not header_names.issubset({"authorization", "content-type", "last-event-id"})
            or private_network not in {None, "true"}
        ):
            self.reply(403, {"error": "preflight request is not allowed"})
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", requested_method)
        self.send_header(
            "Access-Control-Allow-Headers",
            "Authorization, Content-Type, Last-Event-ID",
        )
        if private_network == "true":
            self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header(
            "Vary",
            "Access-Control-Request-Method, Access-Control-Request-Headers, Access-Control-Request-Private-Network",
        )
        self.send_header("Access-Control-Max-Age", "60")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def reply_blob(self, digest):
        blob = read_blob(digest)
        if blob is None:
            self.reply(404, {"error": "image not found"})
            return
        _, body = blob
        try:
            self.send_response(200)
            self.send_header("Content-Type", detect_image_mime(body) or "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        request_path = urlparse(self.path).path
        if not self.host_valid():
            self.reply(400, {"error": "invalid host"})
            return
        if request_path == "/healthz":
            try:
                auth_manager()
                cursors = cursor_store().healthy
            except Exception:
                self.reply(503, {"ok": False, "configuration": True, "authentication": False})
                return
            self.reply(
                200,
                {
                    "ok": True,
                    "configuration": True,
                    "authentication": True,
                    "cursors": cursors,
                },
            )
            return
        if request_path == "/api/client-config":
            self.reply(200, {"mode": "local", "authMode": SETTINGS.auth_mode})
            return
        if request_path.startswith("/api/auth/") and self.headers.get_all("Authorization", []):
            self.reply(403, {"error": "fleet authorization is not allowed for local authentication"})
            return
        if request_path == "/api/auth/session":
            self.handle_auth_get(request_path)
            return
        if request_path.startswith("/api/"):
            try:
                self.request_authenticated()
            except AuthenticationFailed:
                self.reply(401, {"error": "authentication required"})
                return
            except AuthError:
                self.reply(503, {"error": "authentication state unavailable"})
                return
        if request_path == "/api/events":
            send_stream(self, _fleet_model, self.request_authenticated)
            return
        if request_path.startswith("/api/blob/"):
            self.reply_blob(request_path.rsplit("/", 1)[-1])
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
            self.reply(
                200,
                {
                    "projects": launchable_projects(),
                    "rootLabel": f"{DEV_ROOT.name}/",
                    "discoveryDepth": SETTINGS.project_discovery_depth,
                },
            )
            return
        super().do_GET()

    def do_POST(self):
        request_path = urlparse(self.path).path
        if request_path.startswith("/api/auth/"):
            if self.headers.get_all("Authorization", []):
                self.reply(403, {"error": "fleet authorization is not allowed for local authentication"})
                return
            if self.handle_auth_post(request_path):
                return
        if not self.host_valid():
            self.reply(400, {"error": "invalid host"})
            return
        if not self._mutation_origin_valid():
            self.reply(403, {"error": "request origin is not allowed"})
            return
        if not self._authorize_mutation():
            return
        data = self._read_post_body(request_path)
        if data is None:
            return
        # Authentication can expire or be revoked while a bounded body is read.
        # Revalidate at the action boundary rather than dispatching stale authority.
        if not self._authorize_mutation():
            return
        self._normalized_action(request_path, data)

    def _authorize_mutation(self):
        try:
            self.request_authenticated(csrf=True)
            return True
        except AuthenticationFailed:
            self.reply(403, {"error": "authentication and CSRF token required"})
        except AuthError:
            self.reply(503, {"error": "authentication state unavailable"})
        return False

    def _read_post_body(self, request_path):
        limit = (
            2 * 1024 * 1024 if re.fullmatch(r"/api/sessions/[^/]+/(messages|follow-ups)", request_path) else 64 * 1024
        )
        try:
            return self.read_json(limit)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            self.reply(400, {"error": str(error)})
        except TimeoutError:
            self.reply(408, {"error": "request body timed out"})
        return None

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
