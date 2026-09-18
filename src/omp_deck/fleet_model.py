from __future__ import annotations

import copy
import threading
from collections.abc import Mapping
from typing import Any

from ._fleet_extension import project_extension_event
from ._fleet_projection import (
    collab_projection,
    extension_projection,
    now,
    rpc_projection,
)
from ._fleet_registry import registry_session
from ._fleet_rpc_events import project_rpc_event
from ._fleet_unread import latest_assistant_id, project_unread
from ._session_activity import clear_activity
from .event_stream import EventJournal
from .read_cursors import CursorStore
from .rpc_adapter import AdapterEvent, AdapterSnapshot

_PUBLIC_KEYS = (
    "id",
    "lifecycle",
    "ompSessionId",
    "title",
    "displayName",
    "pinned",
    "project",
    "state",
    "transport",
    "historyError",
    "capabilities",
    "attachmentLimits",
    "messages",
    "streamingMessage",
    "deliveryError",
    "todos",
    "subagents",
    "subagentError",
    "pendingUiRequest",
    "unread",
    "updatedAt",
    "workingOn",
    "model",
    "thinking",
    "thinkingLevels",
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
        with self._lock:
            session = registry_session(record, self._sessions.get(session_id))
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

    def update_presentation(self, record: Mapping[str, Any]) -> tuple[dict[str, Any], int]:
        """Apply persisted Deck metadata without claiming or rotating a transport."""
        session_id = str(record["id"])
        revision = int(record.get("presentationRevision", 0))
        with self._lock:
            session = self._sessions[session_id]
            if revision <= session["_presentationRevision"]:
                return self._public_session(session), self._snapshot_generation
            session["displayName"] = record.get("displayName")
            session["pinned"] = bool(record.get("pinned", False))
            session["_presentationRevision"] = revision
            self._changed_locked(
                "session_presentation",
                session_id,
                {"session": self._public_session(session)},
            )
            return self._public_session(session), self._snapshot_generation

    def apply_rpc_snapshot(
        self,
        session_id: str,
        *,
        instance_id: str,
        generation: int,
        snapshot: AdapterSnapshot,
    ) -> bool:
        cursor = self._read_cursor(session_id)
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or not self._may_claim(session, instance_id, generation):
                return False
            if self._owns(session, instance_id, generation) and snapshot.sequence < max(
                session["_adapterSequence"], session["_resyncWatermark"]
            ):
                return False
            self._apply_projection(session, rpc_projection(session, snapshot, session_id, instance_id, generation))
            self._sync_unread_locked(session, cursor)
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
        cursor = self._read_cursor(session_id)
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or not self._may_claim(session, instance_id, generation):
                return False
            if self._owns(session, instance_id, generation) and snapshot.sequence < session["_adapterSequence"]:
                return False
            self._apply_projection(session, collab_projection(session, snapshot, session_id, instance_id, generation))
            self._sync_unread_locked(session, cursor)
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
            if self._owns(session, instance_id, generation) and snapshot.sequence < max(
                session["_adapterSequence"], session["_resyncWatermark"]
            ):
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
        needs_cursor = kind in {"history", "degraded"} or (kind == "message" and payload.get("phase") == "end")
        cursor = self._read_cursor(session_id) if needs_cursor else None
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
            messages = session["messages"]
            project_extension_event(session, session_id, kind, payload)
            if needs_cursor:
                self._sync_unread_locked(session, cursor)
            patch = {key: session[key] for key in _PUBLIC_KEYS if key not in {"id", "lifecycle", "messages"}}
            if session["messages"] is not messages:
                patch["messages"] = session["messages"]
            self._snapshot_generation += 1
            self.journal.publish(
                snapshot_generation=self._snapshot_generation,
                event_type=kind,
                payload={"sessionPatch": patch},
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
            changed = project_rpc_event(session, session_id, event)
            if "messages" in changed:
                project_unread(session, session.get("_readCursor"))
                changed.add("unread")
            self._snapshot_generation += 1
            self.journal.publish(
                snapshot_generation=self._snapshot_generation,
                event_type=event.type,
                payload={"sessionPatch": {key: session[key] for key in changed}},
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
                session["streamingMessage"] = None
                session["_rpcStreamingIndex"] = None
                session["_rpcStreamingKey"] = None
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
            message_id = latest_assistant_id(session)
        if self.cursor_store is not None and message_id is not None:
            self.cursor_store.mark_read(session_id, message_id)
        with self._lock:
            session = self._sessions[session_id]
            session["_readRevision"] = session.get("_readRevision", 0) + 1
            session["_readCursor"] = message_id
            session["unread"] = latest_assistant_id(session) != message_id
            session["updatedAt"] = now()
            self._changed_locked("session_read", session_id, {})
            return self._public_session(session)

    def _read_cursor(self, session_id: str) -> tuple[int, str | None] | None:
        if self.cursor_store is None:
            return None
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            revision = session.get("_readRevision", 0)
        return revision, self.cursor_store.last_read(session_id)

    def _sync_unread_locked(self, session: dict[str, Any], cursor: tuple[int, str | None] | None) -> None:
        if cursor is None:
            return
        revision, message_id = cursor
        # A completed mark_read wins over an earlier storage read that was
        # waiting for this lock. Filesystem I/O never runs inside this section.
        if revision == session.get("_readRevision", 0):
            session["_readCursor"] = message_id
        project_unread(session, session.get("_readCursor"))

    def supports(self, session_id: str, capability: str) -> bool:
        with self._lock:
            return bool(self._sessions[session_id]["capabilities"].get(capability, False))

    def supports_thinking_level(self, session_id: str, level: str) -> bool:
        with self._lock:
            session = self._sessions[session_id]
            return bool(
                session["capabilities"].get("setThinking", False) and level in session.get("thinkingLevels", ())
            )

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            sessions = [self._public_session(session) for session in self._sessions.values()]
            sessions.sort(
                key=lambda value: (
                    not value["pinned"],
                    value["state"] != "needs_input",
                    (value["displayName"] or value["title"]).casefold(),
                    value["id"],
                )
            )
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
        session["streamingMessage"] = None
        session["_rpcStreamingIndex"] = None
        session["_rpcStreamingKey"] = None
        session["subagentError"] = None
        clear_activity(session)
        session["subagents"] = []

    @staticmethod
    def _lifecycle(session: Mapping[str, Any]) -> str:
        return (
            "ongoing" if session["transport"]["instanceId"] is not None or session["_desiredRpcRunning"] else "history"
        )

    @staticmethod
    def _public_session(session: Mapping[str, Any]) -> dict[str, Any]:
        public = {key: session[key] for key in _PUBLIC_KEYS if key != "lifecycle"}
        public["lifecycle"] = FleetModel._lifecycle(session)
        return copy.deepcopy(public)

    def _changed_locked(self, event_type: str, session_id: str, payload: Mapping[str, Any]) -> None:
        self._snapshot_generation += 1
        session = self._sessions.get(session_id)
        if session is not None and "session" not in payload and "sessionPatch" not in payload:
            fields = {
                "session_read": ("unread", "updatedAt"),
                "history_error": ("historyError", "updatedAt"),
            }.get(
                event_type,
                (
                    "state",
                    "transport",
                    "pendingUiRequest",
                    "workingOn",
                    "subagents",
                    "subagentError",
                    "streamingMessage",
                    "updatedAt",
                ),
            )
            patch = {key: session[key] for key in fields}
            patch["lifecycle"] = self._lifecycle(session)
            payload = {**payload, "sessionPatch": patch}
        self.journal.publish(
            snapshot_generation=self._snapshot_generation,
            event_type=event_type,
            payload=payload,
            session_id=session_id,
            instance_id=session["transport"]["instanceId"] if session else None,
            generation=session["_generation"] if session else None,
            sequence=session["_adapterSequence"] if session else None,
        )
