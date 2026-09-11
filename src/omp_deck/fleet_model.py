from __future__ import annotations

import copy
import threading
from collections.abc import Mapping
from typing import Any

from ._fleet_extension import project_extension_event
from ._fleet_projection import (
    collab_projection,
    extension_projection,
    has_visible_content,
    now,
    pending_ui_projection,
    plain_json,
    platform_capabilities,
    rpc_projection,
    strict_clone,
)
from ._session_activity import clear_activity, completion_state, update_tool_activity
from .event_stream import EventJournal
from .read_cursors import CursorStore
from .rpc_adapter import AdapterEvent, AdapterSnapshot

_PUBLIC_KEYS = (
    "id",
    "lifecycle",
    "ompSessionId",
    "title",
    "project",
    "state",
    "transport",
    "historyError",
    "capabilities",
    "messages",
    "todos",
    "subagents",
    "pendingUiRequest",
    "unread",
    "updatedAt",
    "workingOn",
    "model",
    "thinking",
)


class FleetModel:
    """Single authority for normalized session state and event ordering."""

    def __init__(
        self,
        journal: EventJournal | None = None,
        cursor_store: CursorStore | None = None,
    ) -> None:
        self.journal = journal or EventJournal()
        self.cursor_store = cursor_store
        self._lock = threading.RLock()
        self._sessions: dict[str, dict[str, Any]] = {}
        self._snapshot_generation = 0

    def load_record(self, record: Mapping[str, Any]) -> None:
        session_id = str(record["id"])
        adapter_kind = str(record["adapterKind"])
        project = record.get("project")
        if not isinstance(project, Mapping):
            raise ValueError("registry project must be an object")
        with self._lock:
            existing = self._sessions.get(session_id)
            live = existing is not None and existing["transport"]["instanceId"] is not None
            session = {
                "id": session_id,
                "ompSessionId": existing.get("ompSessionId") if live else record.get("ompSessionId"),
                "title": str(record["title"]),
                "project": {"name": str(project["name"]), "path": str(project["path"])},
                "state": existing.get("state", "disconnected") if existing else "disconnected",
                "transport": copy.deepcopy(existing["transport"])
                if existing
                else {
                    "kind": adapter_kind,
                    "status": "disconnected",
                    "label": "Managed RPC" if adapter_kind == "rpc" else adapter_kind.title(),
                    "instanceId": None,
                    "reason": "not connected",
                },
                "historyError": existing.get("historyError") if live else None,
                "capabilities": platform_capabilities(adapter_kind),
                "messages": copy.deepcopy(existing.get("messages", [])) if existing else [],
                "todos": copy.deepcopy(existing.get("todos", [])) if existing else [],
                "subagents": copy.deepcopy(existing.get("subagents", [])) if existing else [],
                "pendingUiRequest": copy.deepcopy(existing.get("pendingUiRequest")) if existing else None,
                "unread": bool(existing.get("unread", False)) if existing else False,
                "updatedAt": str(record["updatedAt"]),
                "workingOn": existing.get("workingOn") if existing else None,
                "model": copy.deepcopy(existing.get("model")) if existing else None,
                "thinking": existing.get("thinking") if existing else None,
                "_desiredRpcRunning": bool(record.get("desiredRpcRunning", False)),
                "_generation": int(record["generation"]),
                "_adapterSequence": int(existing.get("_adapterSequence", 0)) if live else 0,
                "_resyncRequired": bool(existing.get("_resyncRequired", False)) if live else False,
                "_resyncWatermark": int(existing.get("_resyncWatermark", 0)) if live else 0,
                "_activeTools": copy.deepcopy(existing.get("_activeTools", {})) if live else {},
                "_toolActivityOverflow": bool(existing.get("_toolActivityOverflow", False)) if live else False,
            }
            self._sessions[session_id] = session
            self._changed_locked("session_registered", session_id, {"session": self._public_session(session)})

    def remove(self, session_id: str) -> bool:
        with self._lock:
            if self._sessions.pop(session_id, None) is None:
                return False
            self._changed_locked("session_removed", session_id, {})
        if self.cursor_store is not None:
            self.cursor_store.remove(session_id)
        return True

    def apply_rpc_snapshot(
        self,
        session_id: str,
        *,
        instance_id: str,
        generation: int,
        snapshot: AdapterSnapshot,
    ) -> bool:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or not self._may_claim(session, instance_id, generation):
                return False
            if (
                session["_resyncRequired"]
                and self._owns(session, instance_id, generation)
                and snapshot.sequence < session["_resyncWatermark"]
            ):
                return False
            self._apply_projection(session, rpc_projection(session, snapshot, session_id, instance_id, generation))
            self._sync_unread_locked(session)
            self._changed_locked("session_snapshot", session_id, {"session": self._public_session(session)})
            return True

    def apply_collab_snapshot(
        self,
        session_id: str,
        *,
        instance_id: str,
        generation: int,
        snapshot: Any,
    ) -> bool:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or not self._may_claim(session, instance_id, generation):
                return False
            self._apply_projection(session, collab_projection(session, snapshot, session_id, instance_id, generation))
            self._sync_unread_locked(session)
            self._changed_locked("session_snapshot", session_id, {"session": self._public_session(session)})
            return True

    def apply_extension_snapshot(
        self,
        session_id: str,
        *,
        instance_id: str,
        generation: int,
        registration: Any,
        snapshot: Any,
    ) -> bool:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or not self._may_claim(session, instance_id, generation):
                return False
            session.pop("_extensionHistorySync", None)
            self._apply_projection(
                session, extension_projection(session, registration, snapshot, instance_id, generation)
            )
            self._changed_locked("session_snapshot", session_id, {"session": self._public_session(session)})
            return True

    def set_history_error(
        self,
        session_id: str,
        *,
        instance_id: str,
        generation: int,
        reason: str | None,
    ) -> bool:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or not self._owns(session, instance_id, generation):
                return False
            session["historyError"] = reason
            session["updatedAt"] = now()
            self._changed_locked("history_error", session_id, {"reason": reason})
            return True

    def apply_extension_event(
        self,
        session_id: str,
        *,
        instance_id: str,
        generation: int,
        event: Mapping[str, Any],
    ) -> bool:
        sequence = event.get("sequence")
        kind = event.get("kind")
        data = event.get("data")
        if not isinstance(sequence, int) or isinstance(sequence, bool) or not isinstance(kind, str):
            return False
        payload = data if isinstance(data, Mapping) else {"value": data}
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or not self._owns(session, instance_id, generation):
                return False
            if sequence <= session["_adapterSequence"]:
                return False
            if sequence != session["_adapterSequence"] + 1:
                session["_resyncRequired"] = True
                session["_resyncWatermark"] = sequence
                session["state"] = "degraded"
                session["transport"]["reason"] = "extension event gap"
                self._clear_disconnected_activity(session)
                self._changed_locked(
                    "session_resync_required",
                    session_id,
                    {"expectedSequence": session["_adapterSequence"] + 1, "receivedSequence": sequence},
                )
                return False
            session["_adapterSequence"] = sequence
            session["updatedAt"] = now()
            project_extension_event(session, session_id, kind, payload)
            if kind in {"history", "degraded"} or (kind == "message" and payload.get("phase") == "end"):
                self._sync_unread_locked(session)
            self._snapshot_generation += 1
            self.journal.publish(
                snapshot_generation=self._snapshot_generation,
                event_type=kind,
                payload=strict_clone(plain_json(payload)),
                session_id=session_id,
                instance_id=instance_id,
                generation=generation,
                sequence=sequence,
            )
            return True

    def apply_adapter_event(
        self,
        session_id: str,
        *,
        instance_id: str,
        generation: int,
        event: AdapterEvent,
    ) -> bool:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or not self._owns(session, instance_id, generation):
                return False
            if session["_resyncRequired"]:
                session["_resyncWatermark"] = max(session["_resyncWatermark"], event.sequence)
                return False
            previous = session["_adapterSequence"]
            if event.sequence <= previous:
                return False
            if event.sequence != previous + 1:
                session["_resyncRequired"] = True
                session["_resyncWatermark"] = event.sequence
                session["state"] = "degraded"
                session["transport"]["reason"] = "adapter event gap"
                self._clear_disconnected_activity(session)
                self._changed_locked(
                    "session_resync_required",
                    session_id,
                    {"expectedSequence": previous + 1, "receivedSequence": event.sequence},
                )
                return False
            session["_adapterSequence"] = event.sequence
            session["updatedAt"] = now()
            if event.type == "transport":
                status = event.payload.get("status", "disconnected")
                session["transport"].update(status=status, reason=event.payload.get("reason"))
                if status == "degraded":
                    session["state"] = "degraded"
                    self._clear_disconnected_activity(session)
                elif status != "connected":
                    session["state"] = "disconnected"
                    self._clear_disconnected_activity(session)
            elif event.type == "ui_request":
                pending = pending_ui_projection(event.payload)
                if pending is not None:
                    session["pendingUiRequest"] = pending
                    session["state"] = "needs_input"
            elif event.type in {"ui_request_resolved", "ui_request_cancelled", "ui_request_timeout"}:
                pending = session.get("pendingUiRequest")
                if isinstance(pending, Mapping) and pending.get("id") == event.payload.get("id"):
                    session["pendingUiRequest"] = None
                    session["state"] = "working"
            elif event.type == "agent_start":
                clear_activity(session)
                session["state"] = "working"
            elif event.type in {"turn_start", "message_start", "message_update"}:
                if session.get("pendingUiRequest") is None:
                    session["state"] = "working"
            elif event.type == "agent_end" and event.payload.get("is_terminal") is not False:
                session["state"] = completion_state(event.payload.get("messages", ()))
                clear_activity(session)
                # The authoritative transcript refresh owns unread state, not turn settlement.
            elif event.type in {"tool_execution_start", "tool_execution_update", "tool_execution_end"}:
                phase = event.type.removeprefix("tool_execution_")
                update_tool_activity(
                    session,
                    phase,
                    event.payload.get("tool_call_id"),
                    event.payload.get("tool_name"),
                    event.payload.get("intent"),
                )
                if phase != "end" and session.get("pendingUiRequest") is None:
                    session["state"] = "working"
            self._snapshot_generation += 1
            self.journal.publish(
                snapshot_generation=self._snapshot_generation,
                event_type=event.type,
                payload=event.payload,
                session_id=session_id,
                instance_id=instance_id,
                generation=generation,
                sequence=event.sequence,
            )
            return True

    def set_transport(
        self,
        session_id: str,
        *,
        instance_id: str | None,
        generation: int,
        status: str,
        reason: str | None,
    ) -> bool:
        public_status = "reconnecting" if status == "connecting" else status
        with self._lock:
            session = self._sessions[session_id]
            if instance_id is not None and not self._may_claim(session, instance_id, generation):
                return False
            if instance_id is None and generation < session["_generation"]:
                return False
            if instance_id != session["transport"]["instanceId"] or generation != session["_generation"]:
                session["_adapterSequence"] = 0
                session["_resyncRequired"] = False
                session["_resyncWatermark"] = 0
            session["_generation"] = generation
            session["transport"].update(
                status=public_status,
                instanceId=instance_id,
                reason=reason,
            )
            if public_status == "degraded":
                session["state"] = "degraded"
                self._clear_disconnected_activity(session)
            elif public_status != "connected":
                session["state"] = "disconnected"
                self._clear_disconnected_activity(session)
            session["updatedAt"] = now()
            self._changed_locked("transport", session_id, {"status": public_status, "reason": reason})
            return True

    def mark_read(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            session = self._sessions[session_id]
            message_id = self._latest_assistant_id(session)
        if self.cursor_store is not None and message_id is not None:
            self.cursor_store.mark_read(session_id, message_id)
        with self._lock:
            session = self._sessions[session_id]
            session["unread"] = self._latest_assistant_id(session) != message_id
            session["updatedAt"] = now()
            self._changed_locked("session_read", session_id, {})
            return self._public_session(session)

    def _sync_unread_locked(self, session: Mapping[str, Any]) -> None:
        if self.cursor_store is None:
            return
        message_id = self._latest_assistant_id(session)
        cursor = self.cursor_store.last_read(str(session["id"]))
        if message_id is None:
            session["unread"] = False
            return
        if cursor == message_id:
            session["unread"] = False
            return
        messages = session["messages"]
        cursor_index = next(
            (index for index, message in enumerate(messages) if message.get("id") == cursor),
            None,
        )
        latest_index = next(
            index for index, message in reversed(tuple(enumerate(messages))) if message.get("id") == message_id
        )
        session["unread"] = cursor_index is None or latest_index > cursor_index

    @staticmethod
    def _latest_assistant_id(session: Mapping[str, Any]) -> str | None:
        for message in reversed(session["messages"]):
            if (
                message.get("role") == "assistant"
                and isinstance(message.get("id"), str)
                and has_visible_content(message)
            ):
                return message["id"]
        return None

    def supports(self, session_id: str, capability: str) -> bool:
        with self._lock:
            return bool(self._sessions[session_id]["capabilities"].get(capability, False))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            sessions = [self._public_session(session) for session in self._sessions.values()]
            sessions.sort(key=lambda value: (value["state"] != "needs_input", value["title"].casefold(), value["id"]))
            return {
                "generatedAt": now(),
                "snapshotGeneration": self._snapshot_generation,
                "lastEventId": self.journal.latest_id,
                "sessions": sessions,
            }

    def get(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            try:
                return self._public_session(self._sessions[session_id])
            except KeyError as error:
                raise KeyError(session_id) from error

    @staticmethod
    def _owns(session: Mapping[str, Any], instance_id: str, generation: int) -> bool:
        return session["_generation"] == generation and session["transport"]["instanceId"] == instance_id

    @staticmethod
    def _may_claim(session: Mapping[str, Any], instance_id: str, generation: int) -> bool:
        if generation < session["_generation"]:
            return False
        if generation == session["_generation"]:
            owner = session["transport"]["instanceId"]
            return owner is None or owner == instance_id
        return True

    @staticmethod
    def _apply_projection(session: dict[str, Any], projection: dict[str, Any]) -> None:
        if any(session.get(key) != value for key, value in projection.items() if not key.startswith("_")):
            session["updatedAt"] = now()
        session.update(projection)
        if session["transport"]["status"] != "connected":
            FleetModel._clear_disconnected_activity(session)
        elif session["state"] not in {"working", "needs_input"}:
            clear_activity(session)

    @staticmethod
    def _clear_disconnected_activity(session: dict[str, Any]) -> None:
        session["pendingUiRequest"] = None
        clear_activity(session)
        session["subagents"] = []

    @staticmethod
    def _public_session(session: Mapping[str, Any]) -> dict[str, Any]:
        public = {key: session[key] for key in _PUBLIC_KEYS if key != "lifecycle"}
        public["lifecycle"] = (
            "ongoing" if session["transport"]["instanceId"] is not None or session["_desiredRpcRunning"] else "history"
        )
        return copy.deepcopy(public)

    def _changed_locked(self, event_type: str, session_id: str, payload: Mapping[str, Any]) -> None:
        self._snapshot_generation += 1
        session = self._sessions.get(session_id)
        self.journal.publish(
            snapshot_generation=self._snapshot_generation,
            event_type=event_type,
            payload=payload,
            session_id=session_id,
            instance_id=session["transport"]["instanceId"] if session else None,
            generation=session["_generation"] if session else None,
            sequence=session["_adapterSequence"] if session else None,
        )
