import json
import os
import re
import subprocess
import time
from pathlib import Path


def clean_label(title, cwd):
    label = re.sub(r"^π\s*[:>]\s*", "", title).strip()
    return label or Path(cwd).name or cwd


def explicit_resume_path(command, session_root):
    for index, argument in enumerate(command[1:], start=1):
        if argument == "--resume":
            if index + 1 >= len(command):
                return None
            candidate = command[index + 1]
        elif argument.startswith("--resume="):
            candidate = argument.partition("=")[2]
        else:
            continue
        try:
            path = Path(candidate).resolve(strict=True)
            root = session_root.resolve(strict=True)
        except (OSError, RuntimeError):
            return None
        return path if path.suffix == ".jsonl" and path.is_relative_to(root) else None
    return None

_SESSION_ID_SUFFIX = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$",
    re.IGNORECASE,
)


def parent_session_id(session_path, session_root):
    if session_path is None:
        return None
    try:
        relative = session_path.resolve().relative_to(session_root.resolve())
    except (OSError, RuntimeError, ValueError):
        return None
    if len(relative.parts) <= 2:
        return None
    match = _SESSION_ID_SUFFIX.search(relative.parent.name)
    return match.group(1).lower() if match else None


def is_subagent_session(session_path, session_root):
    """Return whether a session lives below a top-level project session directory."""
    if parent_session_id(session_path, session_root) is not None:
        return True
    if session_path is None:
        return False
    try:
        relative = session_path.resolve().relative_to(session_root.resolve())
    except (OSError, RuntimeError, ValueError):
        return False
    return len(relative.parts) > 2


def process_session_path(process_dir, command, session_root):
    resume_path = explicit_resume_path(command, session_root)
    if resume_path is not None:
        return resume_path
    try:
        descriptors = list((process_dir / "fd").iterdir())
    except OSError:
        return None
    for descriptor in descriptors:
        try:
            target = descriptor.resolve()
        except OSError:
            continue
        if target.suffix == ".jsonl" and session_root in target.parents:
            return target
    return None


def omp_processes(session_root, read_header):
    items = []
    seen_sessions = set()
    for process_dir in Path("/proc").iterdir():
        if not process_dir.name.isdigit():
            continue
        try:
            command = [
                os.fsdecode(argument)
                for argument in (process_dir / "cmdline").read_bytes().split(b"\0")
                if argument
            ]
            if Path(command[0]).name != "omp":
                continue
            cwd = str((process_dir / "cwd").resolve())
            session_path = process_session_path(process_dir, command, session_root)
        except (OSError, IndexError):
            continue
        if session_path is None:
            continue
        header = read_header(session_path)
        session_id = header.get("sessionId")
        if not session_id or session_id in seen_sessions:
            continue
        seen_sessions.add(session_id)
        items.append(
            {
                "id": session_id,
                "pid": int(process_dir.name),
                "label": clean_label(header.get("title", ""), cwd),
                "project": Path(cwd).name or cwd,
                "cwd": cwd,
                "sessionHeader": header,
                "parentId": parent_session_id(session_path, session_root),
            }
        )
    return items


def socket_registered_processes(socket_root, session_root, read_header):
    items = []
    for metadata_path in socket_root.glob("*.sock.json"):
        socket_path = Path(str(metadata_path)[:-5])
        try:
            metadata = json.loads(metadata_path.read_text())
            pid = int(metadata["pid"])
            process_dir = Path("/proc") / str(pid)
            command = [
                os.fsdecode(argument)
                for argument in (process_dir / "cmdline").read_bytes().split(b"\0")
                if argument
            ]
            if Path(command[0]).name != "omp" or not socket_path.is_socket():
                continue
            cwd = str((process_dir / "cwd").resolve())
            session_id = str(metadata["sessionId"])
            session_file = metadata.get("sessionFile")
            candidate = Path(session_file).resolve() if session_file else None
            root = session_root.resolve(strict=True)
            if candidate is not None and (
                candidate.suffix != ".jsonl" or root not in candidate.parents
            ):
                continue
            session_path = candidate if candidate is not None and candidate.is_file() else None
        except (KeyError, ValueError, OSError, RuntimeError, json.JSONDecodeError):
            continue
        header = read_header(session_path) if session_path else {
            "path": None,
            "title": metadata.get("title") or "",
            "sessionId": session_id,
            "model": metadata.get("model"),
            "thinking": metadata.get("thinking"),
            "registeredAt": metadata.get("registeredAt"),
        }
        items.append({
            "id": session_id,
            "pid": pid,
            "label": clean_label(header.get("title", ""), cwd),
            "project": Path(cwd).name or cwd,
            "cwd": cwd,
            "sessionHeader": header,
            "parentId": parent_session_id(session_path, session_root),
        })
    return items

def process_tmux_pane(pid):
    entries = (Path("/proc") / str(pid) / "environ").read_bytes().split(b"\0")
    prefix = b"TMUX_PANE="
    return next(
        (os.fsdecode(entry.removeprefix(prefix)) for entry in entries if entry.startswith(prefix)),
        None,
    )




def send_slash_command(agent, command):
    pane = process_tmux_pane(agent["pid"])
    if not pane:
        raise ConnectionError("slash commands require OMP to run inside tmux")
    steps = (("/", 0.3), (command[1:], 0.1), ("Enter", 0.35))
    for index, (keys, delay) in enumerate(steps):
        args = ["tmux", "send-keys", "-t", pane]
        if index < 2:
            args.append("-l")
        result = subprocess.run([*args, keys], text=True, capture_output=True, timeout=3)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or "could not submit slash command")
        if delay:
            time.sleep(delay)

