from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from .auth import AuthenticationFailed, AuthError
from .event_stream import ReplayUnavailableError


def send_stream(handler: Any, fleet_model: Any, authenticate: Callable[[], Any]) -> None:
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
            initial = [(event.id, event.encoded) for event in replay]

    try:
        authenticate()
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
            authenticate()
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
            if replacement is not None:
                authenticate()
                event_id, payload = replacement[0]
                _write_event(handler, journal, event_id, payload)
                continue
            if not events:
                authenticate()
                handler.wfile.write(b": keepalive\n\n")
                handler.wfile.flush()
                continue
            for event in events:
                authenticate()
                _write_event(handler, journal, event.id, event.encoded)
                cursor = event.id
    except AuthError:
        handler.close_connection = True
        return
    except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
        return


def _snapshot_event(fleet_model: Any) -> tuple[int, list[tuple[int, dict[str, Any]]]]:
    snapshot = fleet_model.snapshot()
    cursor = snapshot["lastEventId"]
    return cursor, [(cursor, {"id": cursor, "type": "snapshot", "payload": snapshot})]


def _write_event(handler: Any, journal: Any, event_id: int, payload: Any) -> None:
    data = (
        payload
        if isinstance(payload, bytes)
        else json.dumps(payload, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    )
    frame = b"".join((f"id: {journal.cursor(event_id)}\nevent: fleet\ndata: ".encode(), data, b"\n\n"))
    handler.wfile.write(frame)
    handler.wfile.flush()
