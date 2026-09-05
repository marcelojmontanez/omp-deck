import re

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


class NormalizedActionDispatcher:
    def __init__(self, *, fleet_model, session_root, launch_project, dispatch_command, terminal_manager):
        self.fleet_model = fleet_model
        self.session_root = session_root
        self.launch_project = launch_project
        self.dispatch_command = dispatch_command
        self.terminal_manager = terminal_manager

    def dispatch(self, request_path, data):
        if request_path == "/api/terminal-sessions/collab":
            return self._add_collab(data)
        match = _REMOVE_ROUTE.fullmatch(request_path)
        if match:
            return self._remove(match)
        match = _PROJECT_ROUTE.fullmatch(request_path)
        if match:
            return self._launch(match, data)
        match = _READ_ROUTE.fullmatch(request_path)
        if match:
            return self._mark_read(match, data)
        match = _UI_ROUTE.fullmatch(request_path)
        if match:
            return self._ui_action(match, data)
        match = _ACTION_ROUTE.fullmatch(request_path)
        if match:
            return self._session_action(match, data)
        return None

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
        if payload.get("images") and not self.fleet_model.supports(session_id, "attachments"):
            return 409, _failed(request_id, "attachments_unsupported")
        result = self.dispatch_command(session_id, request_id, action, payload)
        if result["status"] == "accepted":
            return 202, result
        if ui or result["status"] in {"stale", "cancelled"}:
            return 409, result
        return 502, result


def _optional_title(data):
    title = data.get("title")
    if title is None:
        return None
    return bounded_text(title.strip(), "title", 200)


def _failed(request_id, error_code):
    return {"requestId": request_id, "status": "failed", "errorCode": error_code}
