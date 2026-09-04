from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .fleet_model import FleetModel
from .projects import create_project_directory, resolve_project_path
from .request_ledger import RequestLedger
from .rpc_adapter import AdapterError
from .settings import Settings


def _fingerprint(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


class RpcRequestCoordinator:
    """Reserves durable request IDs before RPC launches and commands mutate state."""

    def __init__(
        self, settings: Settings, model: FleetModel, ledger: RequestLedger
    ) -> None:
        self.settings = settings
        self.model = model
        self.ledger = ledger
        self._lock = threading.Lock()
        self._launch_locks: dict[str, threading.Lock] = {}
        self._command_locks: dict[str, threading.Lock] = {}

    def launch(
        self,
        request_id: str,
        project_name: str,
        *,
        title: str | None,
        create: bool,
        start: Callable[..., dict[str, Any]],
    ) -> dict[str, Any]:
        project_path = resolve_project_path(
            self.settings.project_root, project_name, must_exist=not create
        )
        fingerprint = _fingerprint(
            {
                "project": project_name,
                "path": str(project_path),
                "title": title,
                "create": create,
            }
        )
        with self._lock:
            launch_lock = self._launch_locks.setdefault(
                project_name, threading.Lock()
            )
        with launch_lock:
            persisted = self.ledger.reserve("rpc-launch", request_id, fingerprint)
            if persisted is not None:
                if persisted["status"] == "accepted":
                    return {
                        "requestId": request_id,
                        "status": "accepted",
                        "session": self.model.get(persisted["sessionId"]),
                        "created": persisted["created"],
                    }
                return {**persisted, "created": False}
            created = False
            try:
                if create:
                    project_path = create_project_directory(
                        self.settings.project_root, project_name
                    )
                    created = True
                session = start(project_name, project_path, title=title)
                result = {
                    "requestId": request_id,
                    "status": "accepted",
                    "session": session,
                    "created": created,
                }
                durable = {
                    "requestId": request_id,
                    "status": "accepted",
                    "sessionId": session["id"],
                    "created": created,
                }
            except Exception:
                if created:
                    project_path.rmdir()
                result = {
                    "requestId": request_id,
                    "status": "failed",
                    "errorCode": "launch_failed",
                    "created": False,
                }
                durable = {
                    "requestId": request_id,
                    "status": "failed",
                    "errorCode": "launch_failed",
                }
            self.ledger.complete("rpc-launch", request_id, fingerprint, durable)
            return result

    def dispatch(
        self,
        session_id: str,
        request_id: str,
        action: str,
        payload: Mapping[str, Any],
        *,
        execute: Callable[[str, str, Mapping[str, Any]], dict[str, Any]],
    ) -> dict[str, Any]:
        fingerprint = _fingerprint({"action": action, "payload": payload})
        with self._lock:
            command_lock = self._command_locks.setdefault(
                session_id, threading.Lock()
            )
        with command_lock:
            scope = f"rpc-command:{session_id}"
            persisted = self.ledger.reserve(scope, request_id, fingerprint)
            if persisted is not None:
                return persisted
            try:
                command_result = execute(session_id, action, payload)
                outcome = command_result.get("result")
                status = "stale" if outcome == "stale" else "accepted"
                result = {
                    "requestId": request_id,
                    "status": status,
                    "result": outcome,
                }
            except AdapterError as error:
                status = "cancelled" if error.kind == "cancelled" else "failed"
                result = {
                    "requestId": request_id,
                    "status": status,
                    "errorCode": error.kind,
                }
            durable = dict(result)
            if durable.get("status") == "accepted":
                durable["result"] = None
            self.ledger.complete(scope, request_id, fingerprint, durable)
            return result
