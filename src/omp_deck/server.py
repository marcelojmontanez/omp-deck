#!/usr/bin/env python3
import json
import re
import threading
from urllib.parse import urlparse, urlsplit

from . import _draft_http, build_identity
from ._fleet_trust import FleetTrustStore
from ._http_common import DeckHttpHandler, node_preflight_route_known
from ._http_common import ThreadingHTTPServer as ThreadingHTTPServer
from ._rpc_dispatch import SessionCommandConflictError, SessionNotConnectedError
from ._sse import send_stream
from ._terminal_commands import TerminalCommandConflict
from .attachments import MAX_IMAGE_REQUEST_BYTES, detect_image_mime, read_blob
from .auth import COOKIE_NAME as COOKIE_NAME
from .auth import AuthenticationFailed, AuthError, AuthManager, validate_auth_state
from .auth_policy import AuthPolicy
from .fleet_model import FleetModel
from .fleet_protocol import verify_ticket
from .normalized_actions import NormalizedActionDispatcher
from .projects import list_launchable_projects, resolve_project_path
from .read_cursors import CursorStore
from .registry_store import RegistryError, RegistryStore
from .rpc_sessions import RpcSessionManager
from .settings import load_settings
from .terminal_sessions import TerminalSessionManager, TerminalSessionNotConnected
from .usage import UsageService

_IMAGE_REQUEST_BODY_TIMEOUT = 120.0
_IMAGE_RESPONSE_TIMEOUT = 120.0
_IMAGE_REQUEST_ROUTE = re.compile(r"/api/sessions/[^/]+/(messages|follow-ups|draft)")


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
_registry_store = None
_registry_store_lock = threading.Lock()
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


def registry_store():
    global _registry_store
    expected_path = SETTINGS.state_root / "registry.json"
    if _registry_store is None or _registry_store.path != expected_path:
        with _registry_store_lock:
            if _registry_store is None or _registry_store.path != expected_path:
                _registry_store = RegistryStore(expected_path)
    return _registry_store


def rpc_manager():
    global _rpc_manager
    with _rpc_manager_lock:
        if _rpc_manager is None:
            _rpc_manager = RpcSessionManager(SETTINGS, _fleet_model, registry=registry_store())
        return _rpc_manager


def terminal_manager():
    global _terminal_manager
    with _terminal_manager_lock:
        if _terminal_manager is None:
            _terminal_manager = TerminalSessionManager(SETTINGS, _fleet_model, registry=registry_store())
        return _terminal_manager


def start_runtime():
    auth_manager()
    cursor_store()
    terminal_manager().start()
    rpc_manager().request_refresh_connected()


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
    return rpc_manager().launch_request(
        request_id,
        project,
        title=title,
        create=create,
    )


def start_fresh_session(source_session_id, request_id):
    return rpc_manager().fresh_request(source_session_id, request_id)


def dispatch_session_command(session_id, request_id, action, payload):
    session = _fleet_model.get(session_id)
    if session["transport"]["kind"] == "rpc":
        return rpc_manager().dispatch(session_id, request_id, action, payload)
    return terminal_manager().command(session_id, request_id, action, payload)


def delivery_manager(session_id):
    session = _fleet_model.get(session_id)
    return rpc_manager() if session["transport"]["kind"] == "rpc" else terminal_manager()


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
        registry=registry_store(),
        session_root=SESSION_ROOT,
        launch_project=launch_rpc_project,
        dispatch_command=dispatch_session_command,
        start_fresh=start_fresh_session,
        terminal_manager=terminal_manager,
        delivery_recoveries=lambda session_id: delivery_manager(session_id).deliveries(session_id),
        dismiss_delivery=lambda session_id, request_id: delivery_manager(session_id).dismiss_delivery(
            session_id, request_id
        ),
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
            or not node_preflight_route_known(request_path, requested_method)
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
        self.reply_bytes(
            200,
            body,
            content_type=detect_image_mime(body) or "application/octet-stream",
            write_timeout=_IMAGE_RESPONSE_TIMEOUT,
        )

    def do_GET(self):
        request_path = urlparse(self.path).path
        if not self.host_valid():
            self.reply(400, {"error": "invalid host"})
            return
        if request_path == "/healthz":
            try:
                auth_manager()
                validate_auth_state(SETTINGS.state_root)
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
            self.reply(
                200,
                {
                    "mode": "local",
                    "authMode": SETTINGS.auth_mode,
                    "showHistory": SETTINGS.show_history,
                    "build": build_identity(),
                },
            )
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
                # Native lifecycle events own refresh; a browser read must not poll every transcript.
                self.reply(200, _fleet_model.snapshot())
            except Exception:
                self.reply(503, {"error": "session state unavailable"})
            return
        if _draft_http.handle_draft_get(self, request_path, _fleet_model, SETTINGS):
            return
        if request_path.startswith("/api/sessions/") and request_path.endswith("/deliveries"):
            try:
                result = action_dispatcher().recover(request_path)
            except KeyError:
                result = (404, {"error": "session not found"})
            except ValueError:
                result = (400, {"error": "invalid session"})
            except (OSError, RuntimeError):
                result = (503, {"error": "delivery recovery unavailable"})
            self.reply(
                *(result or (404, {"error": "not found"})),
                write_timeout=_IMAGE_RESPONSE_TIMEOUT,
            )
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
        if _draft_http.handle_draft_post(self, request_path, data, _fleet_model, SETTINGS):
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
        image_request = _IMAGE_REQUEST_ROUTE.fullmatch(request_path) is not None
        limit = MAX_IMAGE_REQUEST_BYTES if image_request else 64 * 1024
        try:
            return self.read_json(limit, timeout=_IMAGE_REQUEST_BODY_TIMEOUT if image_request else None)
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
            response = _draft_http.purge_removed_draft(request_path, response, SETTINGS.state_root)
            self.reply(*response)
        except (SessionCommandConflictError, TerminalCommandConflict) as error:
            self.reply(409, {"error": str(error), "errorCode": "request_conflict"})
        except KeyError:
            self.reply(404, {"error": "session not found"})
        except (TypeError, ValueError) as error:
            message = "invalid Collab link" if request_path == "/api/terminal-sessions/collab" else str(error)
            self.reply(400, {"error": message})
        except RegistryError:
            self.reply(503, {"error": "session metadata unavailable"})
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
