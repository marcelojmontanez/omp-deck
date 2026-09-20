from __future__ import annotations

import re
import threading

from .draft_store import (
    DraftCapacityError,
    DraftConflictError,
    DraftCorruptionError,
    DraftStore,
    DraftStoreError,
)
from .request_parsing import decoded_segment

_DRAFT_ROUTE = re.compile(r"/api/sessions/([^/]+)/draft")
_REMOVE_ROUTE = re.compile(r"/api/sessions/([^/]+)/remove")
_store_instance = None
_store_lock = threading.Lock()
_IMAGE_RESPONSE_TIMEOUT = 120.0


def _store(state_root):
    global _store_instance
    expected_root = state_root / "drafts"
    if _store_instance is None or _store_instance.root != expected_root:
        with _store_lock:
            if _store_instance is None or _store_instance.root != expected_root:
                _store_instance = DraftStore(state_root)
    return _store_instance


def handle_draft_get(handler, request_path: str, fleet_model, state_root) -> bool:
    match = _DRAFT_ROUTE.fullmatch(request_path)
    if match is None:
        return False
    try:
        session_id = decoded_segment(match.group(1), "session")
        fleet_model.get(session_id)
        handler.reply(
            200,
            _store(state_root).get(session_id),
            write_timeout=_IMAGE_RESPONSE_TIMEOUT,
        )
    except KeyError:
        handler.reply(404, {"error": "session not found"})
    except ValueError as error:
        handler.reply(400, {"error": str(error)})
    except DraftCorruptionError:
        handler.reply(503, {"error": "draft storage is corrupt"})
    except DraftStoreError:
        handler.reply(503, {"error": "draft storage is unavailable"})
    return True


def handle_draft_post(handler, request_path: str, data, fleet_model, state_root) -> bool:
    match = _DRAFT_ROUTE.fullmatch(request_path)
    if match is None:
        return False
    try:
        if not isinstance(data, dict):
            raise ValueError("request body must be an object")
        required = {"revision", "enabled", "text"}
        if not required.issubset(data) or not set(data).issubset(required | {"images"}):
            raise ValueError("draft body must contain revision, enabled, text, and optional images")
        session_id = decoded_segment(match.group(1), "session")
        fleet_model.get(session_id)
        arguments = {
            "revision": data["revision"],
            "enabled": data["enabled"],
            "text": data["text"],
        }
        if "images" in data:
            arguments["images"] = data["images"]
        handler.reply(200, _store(state_root).save(session_id, **arguments))
    except DraftConflictError as error:
        handler.reply(
            409,
            {"error": str(error), "draft": error.draft},
            write_timeout=_IMAGE_RESPONSE_TIMEOUT,
        )
    except KeyError:
        handler.reply(404, {"error": "session not found"})
    except ValueError as error:
        handler.reply(400, {"error": str(error)})
    except DraftCapacityError as error:
        handler.reply(507, {"error": str(error)})
    except DraftCorruptionError:
        handler.reply(503, {"error": "draft storage is corrupt"})
    except DraftStoreError:
        handler.reply(503, {"error": "draft storage is unavailable"})
    return True


def purge_removed_draft(request_path: str, response, state_root):
    match = _REMOVE_ROUTE.fullmatch(request_path)
    if match is None or response[0] != 200:
        return response
    try:
        _store(state_root).remove(decoded_segment(match.group(1), "session"))
        return response
    except DraftCorruptionError:
        return 503, {
            "error": "session was removed, but its saved draft cleanup failed because draft storage is corrupt"
        }
    except DraftStoreError:
        return 503, {
            "error": "session was removed, but its saved draft cleanup failed because draft storage is unavailable"
        }
