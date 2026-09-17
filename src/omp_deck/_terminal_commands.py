import hashlib
import json
import threading
from collections import OrderedDict
from collections.abc import Callable, Mapping
from typing import Any

from .request_ledger import RequestConflictError, RequestLedger


class TerminalCommandConflict(ValueError):
    pass


def fingerprint(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


class TerminalCommandCoordinator:
    """Serializes terminal mutations and records their durable outcomes."""

    def __init__(self, model, requests: RequestLedger, *, result_limit=1_000):
        self.model = model
        self.requests = requests
        self._lock = threading.RLock()
        self._locks: dict[str, threading.RLock] = {}
        self._priority_locks: dict[tuple[str, str], threading.RLock] = {}
        self._results: OrderedDict[tuple[str, str], tuple[str, dict[str, Any]]] = OrderedDict()
        self._result_limit = result_limit

    def lock(self, session_id: str) -> threading.RLock:
        with self._lock:
            return self._locks.setdefault(session_id, threading.RLock())

    def _command_lock(self, session_id: str, action: str) -> threading.RLock:
        if action not in {"interrupt", "shutdown"}:
            return self.lock(session_id)
        with self._lock:
            return self._priority_locks.setdefault((session_id, action), threading.RLock())

    def forget(self, session_id: str) -> None:
        with self._lock:
            for key in tuple(self._results):
                if key[0] == session_id:
                    self._results.pop(key, None)

    def dispatch(
        self,
        session_id: str,
        request_id: str,
        action: str,
        payload: Mapping[str, Any],
        runtime: Callable[[str], Any],
    ) -> dict[str, Any]:
        request_fingerprint = fingerprint({"action": action, "payload": payload})
        key = (session_id, request_id)
        scope = f"terminal-command:{session_id}"
        with self._command_lock(session_id, action):
            previous = self._cached_result(key, request_fingerprint)
            if previous is not None:
                return previous
            try:
                persisted = self.requests.lookup(scope, request_id, request_fingerprint)
            except RequestConflictError:
                raise TerminalCommandConflict("requestId was already used for another command") from None
            if persisted is not None:
                return persisted
            capability = {
                "sendMessage": "sendMessage",
                "followUp": "followUp",
                "interrupt": "interrupt",
                "shutdown": "shutdown",
                "respondToUi": "answerUiRequest",
                "cancelUi": "cancelUiRequest",
                "setThinking": "setThinking",
            }.get(action)
            if capability is None or not self.model.supports(session_id, capability):
                raise ValueError("action is unavailable for this terminal transport")
            if (
                action == "sendMessage"
                and payload.get("streamingBehavior") == "followUp"
                and not self.model.supports(session_id, "followUp")
            ):
                raise ValueError("follow-up delivery is unavailable for this terminal transport")
            if (
                action in {"sendMessage", "followUp"}
                and payload.get("images")
                and not self.model.supports(session_id, "attachments")
            ):
                raise ValueError("image attachments are unavailable for this terminal transport")
            if action == "setThinking" and not self.model.supports_thinking_level(session_id, payload.get("level")):
                raise ValueError("unsupported thinking level")
            active = runtime(session_id)
            prepared = self._prepare(active, action, payload)
            recovery = self._message_recovery(session_id, action, payload)
            persisted = self.requests.reserve(
                scope,
                request_id,
                request_fingerprint,
                recovery=recovery,
            )
            if persisted is not None:
                return persisted
            try:
                result = self._execute(active, request_id, action, payload, prepared)
            except Exception:
                if recovery is not None:
                    self.requests.mark_uncertain(scope, request_id, request_fingerprint)
                raise
            self.requests.complete(scope, request_id, request_fingerprint, result)
            self._remember(key, request_fingerprint, result)
            return dict(result)

    def deliveries(self, session_id: str) -> list[dict[str, Any]]:
        return self.requests.deliveries(session_id)

    def dismiss_delivery(self, session_id: str, request_id: str) -> bool:
        return self.requests.dismiss_delivery(session_id, request_id)

    @staticmethod
    def _message_recovery(
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
            "images": payload.get("images") or [],
        }

    def _cached_result(
        self,
        key: tuple[str, str],
        request_fingerprint: str,
    ) -> dict[str, Any] | None:
        with self._lock:
            previous = self._results.get(key)
        if previous is None:
            return None
        if previous[0] != request_fingerprint:
            raise TerminalCommandConflict("requestId was already used for another command")
        return dict(previous[1])

    def _remember(
        self,
        key: tuple[str, str],
        request_fingerprint: str,
        result: dict[str, Any],
    ) -> None:
        with self._lock:
            self._results[key] = (request_fingerprint, result)
            self._results.move_to_end(key)
            while len(self._results) > self._result_limit:
                self._results.popitem(last=False)

    @staticmethod
    def _prepare(active, action, payload):
        if action == "followUp" and active.kind != "extension":
            raise ValueError("action is unavailable for this terminal transport")
        if action == "shutdown" and active.kind != "extension":
            raise ValueError("action is unavailable for this terminal transport")
        if action == "setThinking" and active.kind != "extension":
            raise ValueError("action is unavailable for this terminal transport")
        if action not in {"respondToUi", "cancelUi"}:
            return None
        if active.kind != "collab":
            raise ValueError("action is unavailable for this terminal transport")
        try:
            request_id = int(payload["requestId"])
        except (TypeError, ValueError):
            raise ValueError("invalid Collab request id") from None
        value = payload.get("value") if action == "respondToUi" else None
        if value is not None and not isinstance(value, str):
            value = json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        return request_id, value

    def _execute(self, active, request_id, action, payload, prepared):
        adapter = active.adapter
        if action == "sendMessage":
            if active.kind == "extension":
                adapter.send_message(
                    payload["message"],
                    payload.get("streamingBehavior"),
                    images=payload.get("images"),
                )
            else:
                adapter.send_message(payload["message"], images=payload.get("images"))
            return {
                "requestId": request_id,
                "status": "accepted",
                "acceptance": "accepted",
            }
        elif action == "followUp":
            adapter.send_message(payload["message"], "followUp", images=payload.get("images"))
            return {
                "requestId": request_id,
                "status": "accepted",
                "acceptance": "accepted",
            }
        elif action == "interrupt":
            adapter.interrupt()
        elif action == "shutdown":
            adapter.shutdown_session()
        elif action == "setThinking":
            adapter.set_thinking(payload["level"])
            registration = adapter.registration
            snapshot = adapter.refresh()
            if registration is not None and snapshot is not None:
                self.model.apply_extension_snapshot(
                    active.session_id,
                    instance_id=active.instance_id,
                    generation=active.generation,
                    registration=registration,
                    snapshot=snapshot,
                )
        elif prepared is not None:
            numeric_id, value = prepared
            if not adapter.respond_ui(numeric_id, value):
                return {
                    "requestId": request_id,
                    "status": "stale",
                    "result": "stale",
                }
        return {"requestId": request_id, "status": "accepted"}
