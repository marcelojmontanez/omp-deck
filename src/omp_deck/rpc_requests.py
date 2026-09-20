from __future__ import annotations

import copy
import hashlib
import json
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from weakref import WeakValueDictionary

from ._rpc_errors import AdapterError
from ._thinking import normalized_thinking
from .fleet_model import FleetModel
from .projects import create_project_directory, resolve_project_path, resolve_project_reference
from .request_ledger import RequestLedger
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


class FreshSessionMismatchError(RuntimeError):
    pass


class _FreshSourceError(ValueError):
    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


class RpcRequestCoordinator:
    """Reserves durable request IDs before RPC launches and commands mutate state."""

    def __init__(self, settings: Settings, model: FleetModel, ledger: RequestLedger) -> None:
        self.settings = settings
        self.model = model
        self.ledger = ledger
        self._lock = threading.Lock()
        self._launch_locks: dict[str, threading.Lock] = {}
        self._command_locks: dict[str, threading.Lock] = {}
        self._priority_command_locks: dict[str, threading.Lock] = {}
        self._shutdown_command_locks: dict[str, threading.Lock] = {}
        self._fresh_locks: WeakValueDictionary[str, threading.Lock] = WeakValueDictionary()

    def launch(
        self,
        request_id: str,
        project_name: str,
        *,
        title: str | None,
        create: bool,
        start: Callable[..., dict[str, Any]],
    ) -> dict[str, Any]:
        project_path = resolve_project_path(self.settings.project_root, project_name, must_exist=not create)
        fingerprint = _fingerprint(
            {
                "project": project_name,
                "path": str(project_path),
                "title": title,
                "create": create,
            }
        )
        with self._lock:
            launch_lock = self._launch_locks.setdefault(project_name, threading.Lock())
        with launch_lock:
            with self.ledger.admission("rpc-launch", request_id, fingerprint) as persisted:
                if persisted is not None:
                    if persisted["status"] == "accepted":
                        return {
                            "requestId": request_id,
                            "status": "accepted",
                            "session": self.model.get(persisted["sessionId"]),
                            "created": persisted["created"],
                        }
                    return dict(persisted)
                created = False
                try:
                    if create:
                        project_path = create_project_directory(self.settings.project_root, project_name)
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
                    result = {
                        "requestId": request_id,
                        "status": "failed",
                        "errorCode": "launch_failed",
                        "created": created,
                    }
                    durable = dict(result)
                self.ledger.complete("rpc-launch", request_id, fingerprint, durable)
                return result

    def fresh(
        self,
        source_session_id: str,
        request_id: str,
        *,
        start: Callable[..., dict[str, Any]],
    ) -> dict[str, Any]:
        scope = f"rpc-fresh:{source_session_id}"
        fingerprint = _fingerprint({"action": "startFresh", "sourceSessionId": source_session_id})
        with self._lock:
            operation_lock = self._fresh_locks.setdefault(source_session_id, threading.Lock())
        with operation_lock:
            persisted = self.ledger.lookup(scope, request_id, fingerprint)
            if persisted is not None:
                return self._fresh_result(persisted)
            source = self.model.get(source_session_id)
            with self.ledger.admission(scope, request_id, fingerprint) as persisted:
                if persisted is not None:
                    return self._fresh_result(persisted)
                try:
                    project_name, project_path, provider, model_id, thinking, omp_session_id = (
                        self._fresh_source_settings(source)
                    )
                    session = start(
                        project_name,
                        project_path,
                        provider=provider,
                        model=model_id,
                        thinking=thinking,
                        source_session_id=source_session_id,
                        source_omp_session_id=omp_session_id,
                    )
                    result = {
                        "requestId": request_id,
                        "status": "accepted",
                        "session": session,
                    }
                    durable = {
                        "requestId": request_id,
                        "status": "accepted",
                        "sessionId": session["id"],
                    }
                except _FreshSourceError as error:
                    result = {
                        "requestId": request_id,
                        "status": "failed",
                        "errorCode": error.error_code,
                    }
                    durable = dict(result)
                except FreshSessionMismatchError:
                    result = {
                        "requestId": request_id,
                        "status": "failed",
                        "errorCode": "settings_mismatch",
                    }
                    durable = dict(result)
                except Exception:
                    result = {
                        "requestId": request_id,
                        "status": "failed",
                        "errorCode": "launch_failed",
                    }
                    durable = dict(result)
                self.ledger.complete(scope, request_id, fingerprint, durable)
                return result

    def _fresh_source_settings(self, source: Mapping[str, Any]) -> tuple[str, Path, str, str, str, str]:
        capabilities = source.get("capabilities")
        model = source.get("model")
        thinking = normalized_thinking(source.get("thinking"))
        omp_session_id = source.get("ompSessionId")
        if (
            not isinstance(capabilities, Mapping)
            or capabilities.get("startFresh") is not True
            or not isinstance(model, Mapping)
            or not isinstance(model.get("provider"), str)
            or not model["provider"]
            or not isinstance(model.get("id"), str)
            or not model["id"]
            or thinking is None
            or not isinstance(omp_session_id, str)
            or not omp_session_id
        ):
            raise _FreshSourceError("source_settings_unavailable")
        project = source.get("project")
        if not isinstance(project, Mapping):
            raise _FreshSourceError("invalid_project")
        try:
            project_name, project_path = resolve_project_reference(
                self.settings.project_root,
                project.get("path"),
            )
        except ValueError:
            raise _FreshSourceError("invalid_project") from None
        return project_name, project_path, model["provider"], model["id"], thinking, omp_session_id

    def _fresh_result(self, persisted: Mapping[str, Any]) -> dict[str, Any]:
        if persisted["status"] != "accepted":
            return dict(persisted)
        return {
            "requestId": persisted["requestId"],
            "status": "accepted",
            "session": self.model.get(persisted["sessionId"]),
        }

    def dispatch(
        self,
        session_id: str,
        request_id: str,
        action: str,
        payload: Mapping[str, Any],
        *,
        prepare: Callable[[], Any] | None = None,
        execute: Callable[[Any, str, Mapping[str, Any]], dict[str, Any]],
        priority: bool = False,
        shutdown: bool = False,
    ) -> dict[str, Any]:
        fingerprint = _fingerprint({"action": action, "payload": payload})
        scope = f"rpc-command:{session_id}"
        recovery = self._message_recovery(session_id, action, payload)
        with self._lock:
            locks = (
                self._shutdown_command_locks
                if shutdown
                else self._priority_command_locks
                if priority
                else self._command_locks
            )
            command_lock = locks.setdefault(session_id, threading.Lock())
        with command_lock:
            persisted = self.ledger.lookup(scope, request_id, fingerprint)
            if persisted is not None:
                return persisted
            prepared = prepare() if prepare is not None else session_id
            with self.ledger.admission(
                scope,
                request_id,
                fingerprint,
                recovery=recovery,
            ) as persisted:
                if persisted is not None:
                    return persisted
                try:
                    command_result = execute(prepared, action, payload)
                    outcome = command_result.get("result")
                    if isinstance(outcome, Mapping) and outcome.get("cancelled") is True:
                        result = {
                            "requestId": request_id,
                            "status": "cancelled",
                            "errorCode": "cancelled",
                        }
                    else:
                        status = "stale" if outcome == "stale" else "accepted"
                        result = {
                            "requestId": request_id,
                            "status": status,
                            "result": outcome,
                        }
                        if recovery is not None and status == "accepted":
                            result["acceptance"] = "accepted"
                except AdapterError as error:
                    if error.kind not in {"cancelled", "command"}:
                        self.ledger.mark_uncertain(scope, request_id, fingerprint)
                        return {
                            "requestId": request_id,
                            "status": "uncertain",
                            "errorCode": "outcome_unknown",
                        }
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
                return self.ledger.lookup(scope, request_id, fingerprint) or result

    def deliveries(self, session_id: str) -> list[dict[str, Any]]:
        return self.ledger.deliveries(session_id)

    def dismiss_delivery(self, session_id: str, request_id: str) -> bool:
        return self.ledger.dismiss_delivery(session_id, request_id)

    def _message_recovery(
        self,
        session_id: str,
        action: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        if action not in {"sendMessage", "followUp"}:
            return None
        return {
            "kind": "message",
            "sessionId": session_id,
            "action": action,
            "message": payload["message"],
            "images": copy.deepcopy(payload.get("images") or []),
        }
