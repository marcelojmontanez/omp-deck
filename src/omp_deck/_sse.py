from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from .auth import AuthenticationFailed, AuthError
from .event_stream import ReplayUnavailableError


def send_stream(handler: Any, fleet_model: Any, authenticate: Callable, credential: str | None) -> None:
    journal = fleet_model.journal
    raw_cursor = handler.headers.get("Last-Event-ID")
    if raw_cursor is None or raw_cursor == "":
        cursor, initial = _snapshot_event(fleet_model)
    else:
        try:
            cursor, replay = journal.resume(raw_cursor)
        except ValueError:
            handler.reply(400, {"error": "invalid Last-Event-ID"})
            return
        if replay is None:
            cursor, initial = _snapshot_event(fleet_model)
        else:
            initial = [(event.id, event.public()) for event in replay]

    try:
        authenticate(credential)
    except AuthenticationFailed:
        handler.reply(401, {"error": "authentication required"})
        return
    except AuthError:
        handler.reply(503, {"error": "authentication state unavailable"})
        return

    try:
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
        handler.send_header("Connection", "keep-alive")
        handler.send_header("X-Accel-Buffering", "no")
        handler.end_headers()
        handler.connection.settimeout(20)
        for event_id, payload in initial:
            _write_event(handler, journal, event_id, payload)
            cursor = event_id
        while True:
            try:
                events = journal.wait_after(cursor, 15)
            except ReplayUnavailableError:
                cursor, replacement = _snapshot_event(fleet_model)
                events = ()
            else:
                replacement = None
            try:
                authenticate(credential)
            except AuthError:
                handler.close_connection = True
                return
            if replacement is not None:
                event_id, payload = replacement[0]
                _write_event(handler, journal, event_id, payload)
                continue
            if not events:
                handler.wfile.write(b": keepalive\n\n")
                handler.wfile.flush()
                continue
            for event in events:
                _write_event(handler, journal, event.id, event.public())
                cursor = event.id
    except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
        return


def _snapshot_event(fleet_model: Any) -> tuple[int, list[tuple[int, dict[str, Any]]]]:
    snapshot = fleet_model.snapshot()
    cursor = snapshot["lastEventId"]
    return cursor, [(cursor, {"id": cursor, "type": "snapshot", "payload": snapshot})]


def _write_event(handler: Any, journal: Any, event_id: int, payload: Any) -> None:
    data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    frame = f"id: {journal.cursor(event_id)}\nevent: fleet\ndata: {data}\n\n".encode()
    handler.wfile.write(frame)
    handler.wfile.flush()
