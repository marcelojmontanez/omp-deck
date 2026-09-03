#!/usr/bin/env python3
import json
import re
import mimetypes
import socket
import subprocess
import threading
import time
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from attachments import blob_path, cleanup_paths, save_attachments
from projects import list_launchable_projects, resolve_project_path
from session_state import execution_state
from session_projection import (
    MAX_CHAT_MESSAGES,
    blank_session,
    event_time,
    new_session_projection,
    project_event,
    project_message,
    project_tool_result,
    project_tool_start,
    parse_session as _parse_session,
    serialize_session as _serialize_session,
    session_events,
    shorten,
)
import process_discovery
from read_cursors import ReadCursorStore
from settings import SETTINGS

ROOT = Path(__file__).parent
HOST = SETTINGS.host
PORT = SETTINGS.port
HOME = Path.home()
SESSION_ROOT = SETTINGS.session_root
DEV_ROOT = SETTINGS.project_root
OMP_BIN = SETTINGS.omp_bin
SOCKET_ROOT = SETTINGS.socket_root
CURSOR_PATH = SETTINGS.cursor_path
READ_CURSORS = ReadCursorStore(CURSOR_PATH)
_snapshot_lock = threading.Lock()
_snapshot_cache = {"at": 0.0, "value": None}
_usage_lock = threading.Lock()
_usage_cache = {"at": 0.0, "value": None}

def omp_processes():
    registered = process_discovery.socket_registered_processes(
        SOCKET_ROOT, SESSION_ROOT, read_header
    )
    by_session = {agent["id"]: agent for agent in registered}
    for agent in process_discovery.omp_processes(SESSION_ROOT, read_header):
        by_session.setdefault(agent["id"], agent)
    agents = list(by_session.values())
    for agent in agents:
        socket_path = agent_socket_path(agent)
        agent["bridgeConnected"] = socket_path is not None and socket_path.is_socket()
    return agents

def read_header(path):
    header = {"path": path, "title": "", "sessionId": None, "model": None, "thinking": None}
    pinned_title = False
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for _ in range(12):
                line = handle.readline()
                if not line:
                    break
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "title" and event.get("title"):
                    header["title"] = event["title"]
                    pinned_title = True
                elif (
                    event.get("type") == "title_change"
                    and event.get("title")
                    and not pinned_title
                ):
                    header["title"] = event["title"]
                elif event.get("type") == "session":
                    header["sessionId"] = event.get("id")
                    header["title"] = header["title"] or event.get("title", "")
                elif event.get("type") == "model_change":
                    header["model"] = event.get("model")
                elif event.get("type") == "thinking_level_change":
                    header["thinking"] = event.get("thinkingLevel")
    except OSError:
        pass
    return header

def agent_socket_path(agent):
    candidates = sorted(
        SOCKET_ROOT.glob(f"{agent['id']}.{agent['pid']}.*.sock"),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    return candidates[0] if candidates else None

def send_to_agent(agent, message, attachments=(), thinking=None, timeout=5):
    socket_path = agent_socket_path(agent)
    if socket_path is None:
        raise ConnectionError(
            "OMP Deck is not connected to this session; run /reload-plugins, then /omp-deck in OMP"
        )
    request = json.dumps(
        {
            "message": message,
            "attachments": [str(path) for path in attachments],
            "thinking": thinking,
        }
    ).encode() + b"\n"
    response = bytearray()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(str(socket_path))
        client.sendall(request)
        while b"\n" not in response:
            chunk = client.recv(4096)
            if not chunk:
                break
            response.extend(chunk)
            if len(response) > 64 * 1024:
                raise RuntimeError("OMP returned an oversized response")
    try:
        result = json.loads(response.split(b"\n", 1)[0])
    except (json.JSONDecodeError, UnicodeDecodeError, IndexError) as error:
        raise RuntimeError("OMP closed the bridge without accepting the message") from error
    if not result.get("ok"):
        raise RuntimeError(result.get("error") or "OMP rejected the message")

def remove_later(paths, delay=600):
    def remove():
        time.sleep(delay)
        for path in paths:
            path.unlink(missing_ok=True)

    if paths:
        threading.Thread(target=remove, daemon=True).start()

def serialize_session(
    header, projection, archive_activity=None, reset_at=None, event_ids=None
):
    return _serialize_session(
        header,
        projection,
        READ_CURSORS,
        archive_activity,
        reset_at,
        event_ids,
    )

def parse_session(header):
    return _parse_session(header, READ_CURSORS)

def build_snapshot():
    projected = []
    for discovered in omp_processes():
        process = dict(discovered)
        header = process.pop("sessionHeader")
        session = parse_session(header)
        session["executionState"] = execution_state(
            session["status"], process.get("bridgeConnected", False)
        )
        projected.append({
            **process,
            "status": session["status"],
            "executionState": session["executionState"],
            "inboxState": session["inboxState"],
            "activitySummary": session["activitySummary"],
            "session": session,
        })
    by_id = {agent["id"]: agent for agent in projected}
    top_level = []
    for agent in projected:
        if agent.get("parentId") is None:
            agent.setdefault("subagents", [])
            top_level.append(agent)
            continue
        parent = by_id.get(agent["parentId"])
        if parent is None:
            continue
        root = parent
        while root.get("parentId") in by_id:
            root = by_id[root["parentId"]]
        root.setdefault("subagents", []).append({
            "id": agent["id"],
            "label": agent["label"],
            "model": agent["session"]["model"],
            "executionState": agent["executionState"],
            "activitySummary": agent["activitySummary"],
            "updatedMs": agent["session"]["updatedMs"],
        })
    order = {"needs-input": 0, "attention": 1, "working": 2, "idle": 3, "unknown": 4}
    top_level.sort(key=lambda agent: (
        agent["inboxState"] != "unread",
        order.get(agent["status"], 9),
        agent["label"].casefold(),
    ))
    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "source": "omp-session-jsonl",
        "agents": top_level,
    }

def snapshot():
    now = time.monotonic()
    cached = _snapshot_cache["value"]
    if cached is not None and now - _snapshot_cache["at"] < 1:
        return cached
    with _snapshot_lock:
        now = time.monotonic()
        cached = _snapshot_cache["value"]
        if cached is None or now - _snapshot_cache["at"] >= 1:
            cached = build_snapshot()
            _snapshot_cache.update(at=now, value=cached)
        return cached

def usage_snapshot():
    now = time.monotonic()
    cached = _usage_cache["value"]
    if cached is not None and now - _usage_cache["at"] < 60:
        return cached
    with _usage_lock:
        now = time.monotonic()
        cached = _usage_cache["value"]
        if cached is not None and now - _usage_cache["at"] < 60:
            return cached
        try:
            result = subprocess.run(
                [OMP_BIN, "usage", "--json"],
                text=True,
                capture_output=True,
                timeout=15,
            )
            if result.returncode:
                raise RuntimeError(result.stderr.strip() or "OMP usage failed")
            value = json.loads(result.stdout)
        except (OSError, RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError) as error:
            value = {"generatedAt": int(time.time() * 1000), "reports": [], "error": str(error)}
        _usage_cache.update(at=now, value=value)
        return value


def project_path(project, *, must_exist=True):
    return resolve_project_path(DEV_ROOT, project, must_exist=must_exist)


def launchable_projects():
    return list_launchable_projects(DEV_ROOT)


def launch_project(project, create=False):
    if not isinstance(project, str):
        raise ValueError("invalid project")
    project = project.strip()
    path = project_path(project, must_exist=not create)
    created = False
    if create:
        try:
            path.mkdir()
            path = project_path(project)
            created = True
        except FileExistsError as error:
            raise ValueError("project already exists") from error
        except OSError as error:
            raise RuntimeError(f"could not create project: {error}") from error
    elif project not in launchable_projects():
        raise ValueError("invalid project")
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", project).strip("-") or "project"
    session_name = f"omp-{slug[:24]}-{time.time_ns() % 100000000:08d}"
    try:
        result = subprocess.run(
            ["tmux", "new-session", "-d", "-s", session_name, "-c", str(path), OMP_BIN, "--cwd", str(path)],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        if created:
            path.rmdir()
        raise
    if result.returncode:
        if created:
            path.rmdir()
        raise RuntimeError(result.stderr.strip() or "could not start OMP")
    _snapshot_cache.update(at=0, value=None)
    return {"ok": True, "delivery": "accepted", "project": project, "cwd": str(path), "created": created}



def deliver_message(agent, message, attachment_items, thinking):
    paths = save_attachments(attachment_items)
    try:
        send_to_agent(agent, message, paths, thinking)
    except Exception:
        cleanup_paths(paths)
        raise
    remove_later(paths)

def trusted_browser_request(headers):
    if headers.get("Sec-Fetch-Site") == "cross-site":
        return False
    origin = headers.get("Origin")
    if not origin:
        return True
    return urlparse(origin).netloc == headers.get("Host")



class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT / "static"), **kwargs)
    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length > 17 * 1024 * 1024:
            raise ValueError("request exceeds the 17 MB limit")
        return json.loads(self.rfile.read(length))

    def session_agent(self, session_id):
        agent = next((item for item in omp_processes() if item["id"] == session_id), None)
        if agent is None:
            raise ValueError("invalid session")
        return agent

    def mark_read(self, session_id, event_id):
        agent = self.session_agent(session_id)
        session = parse_session(agent["sessionHeader"])
        if event_id not in session["inboxEventIds"]:
            raise ValueError("invalid event")
        event_id = READ_CURSORS.advance(session_id, event_id, session["eventIds"])
        _snapshot_cache.update(at=0, value=None)
        return {"ok": True, "session": session_id, "eventId": event_id, "inboxState": "read"}

    def perform_action(self, session_id, action):
        agent = self.session_agent(session_id)
        if action == "interrupt":
            session = parse_session(agent["sessionHeader"])
            if session["executionState"] != "working":
                raise ValueError("OMP is not currently working")
            pane = process_discovery.process_tmux_pane(agent["pid"])
            if not pane:
                raise ConnectionError("interrupt requires OMP to run inside tmux")
            result = subprocess.run(
                ["tmux", "send-keys", "-t", pane, "Escape", "Escape"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            if result.returncode:
                raise RuntimeError(result.stderr.strip() or "could not interrupt OMP")
            command = None
        elif action in {"clear", "quit"}:
            command = f"/{action}"
            process_discovery.send_slash_command(agent, command)
        else:
            raise ValueError("invalid action")
        _snapshot_cache.update(at=0, value=None)
        return {"ok": True, "delivery": "accepted", "action": action, "command": command}

    def log_message(self, format, *args):
        print(f"{self.client_address[0]} {format % args}")

    def end_headers(self):
        if not urlparse(self.path).path.startswith("/api/"):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def reply(self, status, payload):
        body = json.dumps(payload).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def reply_blob(self, path):
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        request_path = urlparse(self.path).path
        if request_path.startswith("/api/blob/"):
            path = blob_path(f"blob:sha256:{request_path.rsplit('/', 1)[-1]}")
            if path:
                self.reply_blob(path)
            else:
                self.reply(404, {"error": "image not found"})
            return
        if urlparse(self.path).path == "/api/agents":
            self.reply(200, snapshot())
            return
        if urlparse(self.path).path == "/api/sessions":
            self.reply(200, {"sessions": snapshot()["agents"]})
            return
        if urlparse(self.path).path == "/api/usage":
            self.reply(200, usage_snapshot())
            return
        if urlparse(self.path).path == "/api/projects":
            self.reply(200, {"projects": launchable_projects()})
            return
        super().do_GET()
    def do_POST(self):
        request_path = urlparse(self.path).path
        if not trusted_browser_request(self.headers):
            self.reply(403, {"error": "cross-site requests are not allowed"})
            return
        try:
            data = self.read_json()
            session_id = data.get("session")
            read_match = re.fullmatch(r"/api/sessions/([^/]+)/read", request_path)
            action_match = re.fullmatch(r"/api/sessions/([^/]+)/lifecycle", request_path)
            if request_path == "/api/read" or read_match:
                session_id = read_match.group(1) if read_match else session_id
                self.reply(200, self.mark_read(session_id, data.get("eventId")))
                return
            if request_path == "/api/action" or action_match:
                session_id = action_match.group(1) if action_match else session_id
                self.reply(202, self.perform_action(session_id, data.get("action")))
                return
            if request_path == "/api/launch":
                self.reply(202, launch_project(data.get("project"), data.get("create") is True))
                return
            if request_path != "/api/message":
                self.reply(404, {"error": "not found"})
                return
            message = data["message"].strip()
            thinking = data.get("thinking")
            if thinking not in {None, "off", "minimal", "low", "medium", "high", "xhigh", "max", "auto"}:
                raise ValueError("invalid thinking level")
            agent = self.session_agent(session_id)
            if not message or len(message) > 4000:
                raise ValueError("invalid session or message")
            attachment_items = data.get("attachments") or []
            command = None
            if message.startswith("/") and not attachment_items:
                command = message
                process_discovery.send_slash_command(agent, message)
            else:
                deliver_message(agent, message, attachment_items, thinking)
            _snapshot_cache.update(at=0, value=None)
            response = {"ok": True, "delivery": "live"}
            if command:
                response["command"] = command
            self.reply(200, response)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            self.reply(400, {"error": str(error)})
        except ConnectionError as error:
            self.reply(409, {"error": str(error)})
        except (OSError, TimeoutError) as error:
            self.reply(502, {"error": f"could not reach OMP: {error}"})
        except RuntimeError as error:
            self.reply(502, {"error": str(error)})

if __name__ == "__main__":
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"OMP Deck ready at http://{HOST}:{PORT}", flush=True)
    server.serve_forever()
