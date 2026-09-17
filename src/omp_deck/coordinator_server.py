from __future__ import annotations

import json
import signal
import time
from dataclasses import dataclass
from urllib.parse import urlparse

from . import build_identity
from ._http_common import DeckHttpHandler, ThreadingHTTPServer
from .auth import AuthenticationFailed, AuthError, AuthManager, validate_auth_state
from .auth_policy import AuthPolicy
from .coordinator_settings import COORDINATOR_COOKIE_NAME, CoordinatorSettings
from .coordinator_store import CoordinatorStateError, CoordinatorStore
from .fleet_protocol import issue_ticket


@dataclass(frozen=True)
class CoordinatorContext:
    settings: CoordinatorSettings
    store: CoordinatorStore
    auth: AuthManager
    policy: AuthPolicy

    @classmethod
    def create(cls, settings: CoordinatorSettings) -> CoordinatorContext:
        store = CoordinatorStore(settings.state_root, settings.origin)
        auth = AuthManager(settings.state_root, cookie_name=COORDINATOR_COOKIE_NAME)
        policy = AuthPolicy(settings.auth_mode, settings.tailscale_user)
        return cls(settings=settings, store=store, auth=auth, policy=policy)


class CoordinatorHTTPServer(ThreadingHTTPServer):
    def __init__(self, address, context: CoordinatorContext):
        self.context = context
        super().__init__(address, CoordinatorHandler)


class CoordinatorHandler(DeckHttpHandler):
    def handle_one_request(self) -> None:
        self._coordinator_document_access = None
        super().handle_one_request()

    @property
    def context(self) -> CoordinatorContext:
        return self.server.context

    def auth_backend(self) -> AuthManager:
        return self.context.auth

    def auth_policy(self) -> AuthPolicy:
        return self.context.policy

    def cookie_name(self) -> str:
        return COORDINATOR_COOKIE_NAME

    def allowed_hosts(self) -> tuple[str, ...]:
        return self.context.settings.allowed_hosts

    def browser_origin_valid(self) -> bool:
        return self.headers.get_all("Origin", []) == [self.context.settings.origin] and super().browser_origin_valid()

    def _document_access(self) -> tuple[bool, tuple[str, ...]]:
        cached = getattr(self, "_coordinator_document_access", None)
        if cached is not None:
            return cached
        path = urlparse(self.path).path
        if path not in {"/", "/index.html"}:
            result = (False, ())
        else:
            try:
                self.request_authenticated()
                origins = tuple(machine["origin"] for machine in self.context.store.machines())
                result = (True, origins)
            except (AuthError, CoordinatorStateError):
                result = (False, ())
        self._coordinator_document_access = result
        return result

    def connect_origins(self) -> tuple[str, ...]:
        # Roster origins in CSP are private. A newly paired or changed roster is
        # picked up by an explicit document reload with the authenticated cookie.
        return self._document_access()[1]

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if not self.host_valid():
            self.reply(400, {"error": "invalid host"})
            return
        if path == "/healthz":
            self._health()
            return
        if path == "/api/client-config":
            self.reply(
                200,
                {
                    "mode": "coordinator",
                    "authMode": self.context.policy.mode,
                    "showHistory": self.context.settings.show_history,
                    "build": build_identity(),
                },
            )
            return
        if self.handle_auth_get(path):
            return
        if path == "/api/fleet":
            self._fleet()
            return
        if path.startswith("/api/"):
            self.reply(404, {"error": "not found"})
            return
        super().do_GET()

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if self.handle_auth_post(path):
            return
        if not self.host_valid():
            self.reply(400, {"error": "invalid host"})
            return
        if path == "/api/fleet/access":
            self._access()
            return
        self.reply(404, {"error": "not found"})

    def _health(self) -> None:
        try:
            self.context.store.validate_persisted()
        except Exception:
            fleet_healthy = False
        else:
            fleet_healthy = True
        try:
            validate_auth_state(self.context.settings.state_root)
        except Exception:
            auth_healthy = False
        else:
            auth_healthy = True
        healthy = fleet_healthy and auth_healthy
        self.reply(
            200 if healthy else 503,
            {
                "ok": healthy,
                "configuration": True,
                "authentication": auth_healthy,
                "fleet": fleet_healthy,
            },
        )

    def _fleet(self) -> None:
        try:
            self.request_authenticated()
        except AuthenticationFailed:
            self.reply(401, {"error": "authentication required"})
            return
        except AuthError:
            self.reply(503, {"error": "authentication state unavailable"})
            return
        try:
            machines = self.context.store.machines()
        except CoordinatorStateError:
            self.reply(503, {"error": "coordinator state unavailable"})
            return
        self.reply(
            200,
            {
                "version": 1,
                "machines": [
                    {"id": machine["id"], "name": machine["name"], "origin": machine["origin"]} for machine in machines
                ],
            },
        )

    def _access(self) -> None:
        if not self.browser_origin_valid():
            self.reply(403, {"error": "request origin is not allowed"})
            return
        try:
            self.request_authenticated()
        except AuthenticationFailed:
            self.reply(401, {"error": "authentication required"})
            return
        except AuthError:
            self.reply(503, {"error": "authentication state unavailable"})
            return
        try:
            self.request_authenticated(csrf=True)
        except AuthenticationFailed:
            self.reply(403, {"error": "valid CSRF token required"})
            return
        except AuthError:
            self.reply(503, {"error": "authentication state unavailable"})
            return
        try:
            data = self.read_json(4096)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            self.reply(400, {"error": str(error)})
            return
        except TimeoutError:
            self.reply(408, {"error": "request body timed out"})
            return
        if data:
            self.reply(400, {"error": "request body must be empty"})
            return
        # Revalidate after the body read, anchoring the ticket lifetime before
        # that authorization attempt so delayed signing cannot extend access.
        issued_at = int(time.time())
        try:
            device = self.request_authenticated(csrf=True)
        except AuthenticationFailed:
            self.reply(403, {"error": "valid CSRF token required"})
            return
        except AuthError:
            self.reply(503, {"error": "authentication state unavailable"})
            return
        try:
            private_key, machines = self.context.store.access_material()
            grants = []
            for machine in machines:
                token, expires_at = issue_ticket(
                    private_key,
                    issuer=self.context.settings.origin,
                    machine_id=machine["id"],
                    device_id=device.device_id,
                    now=issued_at,
                )
                grants.append(
                    {
                        "id": machine["id"],
                        "name": machine["name"],
                        "origin": machine["origin"],
                        "accessToken": token,
                        "expiresAt": expires_at,
                    }
                )
        except (CoordinatorStateError, ValueError):
            self.reply(503, {"error": "coordinator state unavailable"})
            return
        self.reply(200, {"version": 1, "machines": grants})


def serve(settings: CoordinatorSettings) -> None:
    context = CoordinatorContext.create(settings)
    httpd = CoordinatorHTTPServer((settings.host, settings.port), context)
    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def terminate(_signum, _frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, terminate)
    try:
        print(f"OMP Deck coordinator ready at {settings.origin}", flush=True)
        httpd.serve_forever()
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
        httpd.server_close()
