import re

from .registry_store import MAX_DISPLAY_NAME_LENGTH
from .request_parsing import (
    bounded_text,
    client_request_id,
    decoded_project,
    decoded_segment,
    rpc_command_for_route,
)

_CAPABILITIES = {
    "sendMessage": "sendMessage",
    "followUp": "followUp",
    "interrupt": "interrupt",
    "shutdown": "shutdown",
    "setModel": "setModel",
    "setThinking": "setThinking",
    "branch": "branch",
    "switchSession": "switch",
    "compact": "compact",
    "respondToUi": "answerUiRequest",
    "cancelUi": "cancelUiRequest",
}
_ACTION_ROUTE = re.compile(
    r"/api/sessions/([^/]+)/(messages|follow-ups|interrupt|shutdown|model|thinking|branch|switch|resume|compact)"
)
_UI_ROUTE = re.compile(r"/api/sessions/([^/]+)/ui-requests/([^/]+)/(respond|cancel)")
_PROJECT_ROUTE = re.compile(r"/api/projects/(.+)/sessions")
_READ_ROUTE = re.compile(r"/api/sessions/([^/]+)/read")
_REMOVE_ROUTE = re.compile(r"/api/sessions/([^/]+)/remove")
_PRESENTATION_ROUTE = re.compile(r"/api/sessions/([^/]+)/presentation")
_FRESH_ROUTE = re.compile(r"/api/sessions/([^/]+)/fresh")

_DELIVERY_ROUTE = re.compile(r"/api/sessions/([^/]+)/deliveries")
_DELIVERY_DISMISS_ROUTE = re.compile(r"/api/sessions/([^/]+)/deliveries/([^/]+)/dismiss")


class NormalizedActionDispatcher:
    def __init__(
        self,
        *,
        fleet_model,
        registry,
        session_root,
        launch_project,
        dispatch_command,
        start_fresh,
        terminal_manager,
        delivery_recoveries,
        dismiss_delivery,
    ):
        self.fleet_model = fleet_model
        self.registry = registry
        self.session_root = session_root
        self.launch_project = launch_project
        self.dispatch_command = dispatch_command
        self.start_fresh = start_fresh
        self.terminal_manager = terminal_manager
        self.delivery_recoveries = delivery_recoveries
        self.dismiss_delivery = dismiss_delivery

    def dispatch(self, request_path, data):
        match = _DELIVERY_DISMISS_ROUTE.fullmatch(request_path)
        if match:
            return self._dismiss_delivery(match, data)
        if request_path == "/api/terminal-sessions/collab":
            return self._add_collab(data)
        match = _PRESENTATION_ROUTE.fullmatch(request_path)
        if match:
            return self._update_presentation(match, data)
        match = _REMOVE_ROUTE.fullmatch(request_path)
        if match:
            return self._remove(match)
        match = _PROJECT_ROUTE.fullmatch(request_path)
        if match:
            return self._launch(match, data)
        match = _READ_ROUTE.fullmatch(request_path)
        if match:
            return self._mark_read(match, data)
        match = _FRESH_ROUTE.fullmatch(request_path)
        if match:
            return self._fresh(match, data)
        match = _UI_ROUTE.fullmatch(request_path)
        if match:
            return self._ui_action(match, data)
        match = _ACTION_ROUTE.fullmatch(request_path)
        if match:
            return self._session_action(match, data)
        return None

    def recover(self, request_path):
        match = _DELIVERY_ROUTE.fullmatch(request_path)
        if match is None:
            return None
        session_id = decoded_segment(match.group(1), "session")
        self.fleet_model.get(session_id)
        deliveries = self.delivery_recoveries(session_id)
        return 200, {"version": 1, "deliveries": deliveries}

    def _dismiss_delivery(self, match, data):
        if data:
            raise ValueError("request body must be empty")
        session_id = decoded_segment(match.group(1), "session")
        request_id = decoded_segment(match.group(2), "request")
        self.fleet_model.get(session_id)
        self.dismiss_delivery(session_id, request_id)
        return 200, {"requestId": request_id, "status": "dismissed"}

    def _update_presentation(self, match, data):
        session_id = decoded_segment(match.group(1), "session")
        self.fleet_model.get(session_id)
        changes = {}
        if "displayName" in data:
            display_name = data["displayName"]
            if not isinstance(display_name, str):
                raise ValueError("displayName must be a string")
            display_name = display_name.strip()
            if len(display_name) > MAX_DISPLAY_NAME_LENGTH:
                raise ValueError(f"displayName must be at most {MAX_DISPLAY_NAME_LENGTH} characters")
            changes["display_name"] = display_name or None
        if "pinned" in data:
            if not isinstance(data["pinned"], bool):
                raise ValueError("pinned must be boolean")
            changes["pinned"] = data["pinned"]
        if not changes:
            raise ValueError("displayName or pinned is required")
        record = self.registry.update_presentation(session_id, **changes)
        session, snapshot_generation = self.fleet_model.update_presentation(record)
        return 200, {
            "status": "updated",
            "snapshotGeneration": snapshot_generation,
            "session": session,
        }

    def _add_collab(self, data):
        request_id = client_request_id(data)
        link = bounded_text(data.get("link"), "Collab link", 8192)
        title = _optional_title(data)
        result = self.terminal_manager().add_collab_request(request_id, link, title=title)
        return (201 if result["status"] == "accepted" else 502), result

    def _remove(self, match):
        session_id = decoded_segment(match.group(1), "session")
        try:
            session = self.fleet_model.get(session_id)
        except KeyError:
            session = None
        if session is not None and session["transport"]["kind"] == "rpc":
            return 409, {"error": "session is not removable"}
        if not self.terminal_manager().remove(session_id):
            return 404, {"error": "session not found"}
        return 200, {"status": "removed"}

    def _launch(self, match, data):
        project = decoded_project(match.group(1))
        request_id = client_request_id(data)
        create = data.get("create", False)
        if not isinstance(create, bool):
            raise ValueError("create must be boolean")
        result = self.launch_project(project, request_id, create, _optional_title(data))
        return (201 if result["status"] == "accepted" else 502), result

    def _fresh(self, match, data):
        if set(data) != {"requestId"}:
            raise ValueError("request body must contain only requestId")
        session_id = decoded_segment(match.group(1), "session")
        request_id = client_request_id(data)
        result = self.start_fresh(session_id, request_id)
        if result["status"] == "accepted":
            return 201, result
        if result.get("errorCode") in {"source_settings_unavailable", "invalid_project"}:
            return 409, result
        return 502, result

    def _mark_read(self, match, data):
        session_id = decoded_segment(match.group(1), "session")
        request_id = client_request_id(data)
        return 200, {
            "requestId": request_id,
            "status": "accepted",
            "session": self.fleet_model.mark_read(session_id),
        }

    def _ui_action(self, match, data):
        session_id = decoded_segment(match.group(1), "session")
        ui_request_id = decoded_segment(match.group(2), "UI request")
        request_id = client_request_id(data)
        suffix = "ui-respond" if match.group(3) == "respond" else "ui-cancel"
        action, payload = rpc_command_for_route(suffix, data, self.session_root, ui_request_id)
        return self._dispatch(session_id, request_id, action, payload, ui=True)

    def _session_action(self, match, data):
        session_id = decoded_segment(match.group(1), "session")
        request_id = client_request_id(data)
        action, payload = rpc_command_for_route(match.group(2), data, self.session_root)
        return self._dispatch(session_id, request_id, action, payload)

    def _dispatch(self, session_id, request_id, action, payload, *, ui=False):
        if not self.fleet_model.supports(session_id, _CAPABILITIES[action]):
            return 409, _failed(request_id, "unsupported")
        if action == "setThinking" and not self.fleet_model.supports_thinking_level(session_id, payload["level"]):
            return 409, _failed(request_id, "unsupported")
        if payload.get("images") and not self.fleet_model.supports(session_id, "attachments"):
            return 409, _failed(request_id, "attachments_unsupported")
        result = self.dispatch_command(session_id, request_id, action, payload)
        if result["status"] == "accepted":
            if action in {"sendMessage", "followUp"}:
                result = {**result, "acceptance": result.get("acceptance", "accepted")}
            return 202, result
        if ui or result["status"] in {"stale", "cancelled"}:
            return 409, result
        return 502, result


def _optional_title(data):
    title = data.get("title")
    if title is None:
        return None
    return bounded_text(title.strip() if isinstance(title, str) else title, "title", 200)


def _failed(request_id, error_code):
    return {"requestId": request_id, "status": "failed", "errorCode": error_code}
