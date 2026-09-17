import json
import re
import socket
import threading
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import unquote, urlparse

from ._thinking import REQUEST_THINKING_LEVELS
from .attachments import validated_image_content
from .auth import COOKIE_NAME


def read_json_request(handler, max_bytes=64 * 1024):
    header = handler.headers.get("Content-Length")
    if header is None or not header.isascii() or not header.isdecimal():
        raise ValueError("invalid Content-Length")
    length = int(header)
    if length > max_bytes:
        raise ValueError(f"request exceeds the {max_bytes}-byte limit")
    timeout = handler.server.body_timeout
    previous_timeout = handler.connection.gettimeout()
    expired = threading.Event()
    deadline = threading.Timer(timeout, _expire_body, (handler.connection, expired))
    deadline.daemon = True
    handler.connection.settimeout(min(10.0, timeout))
    deadline.start()
    try:
        body = handler.rfile.read(length)
    finally:
        deadline.cancel()
        handler.connection.settimeout(previous_timeout)
    if expired.is_set():
        raise TimeoutError("request body timed out")
    if len(body) != length:
        raise ValueError("incomplete request body")
    try:
        value = json.loads(body)
    except RecursionError:
        raise ValueError("request JSON nesting is too deep") from None
    if not isinstance(value, dict):
        raise ValueError("request body must be a JSON object")
    return value


def _expire_body(connection, expired):
    expired.set()
    try:
        connection.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


def decoded_segment(value, name):
    value = unquote(value)
    if not value or len(value) > 128 or "/" in value or "\\" in value:
        raise ValueError(f"invalid {name}")
    return value


def decoded_project(value):
    value = unquote(value)
    if not value or len(value) > 240 or "\\" in value or any(ord(character) < 32 for character in value):
        raise ValueError("invalid project")
    return value


def bounded_text(value, name, max_length):
    if (
        not isinstance(value, str)
        or not value
        or len(value) > max_length
        or any(ord(character) < 32 and character not in "\n\t" for character in value)
    ):
        raise ValueError(f"invalid {name}")
    return value


def client_request_id(data):
    request_id = bounded_text(data.get("requestId"), "requestId", 128)
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", request_id) is None:
        raise ValueError("invalid requestId")
    return request_id


def message_payload(data, *, streaming_behavior=None):
    message = data.get("message")
    if not isinstance(message, str):
        raise ValueError("message must be text")
    message = message.strip()
    images = _image_payloads(data.get("attachments") or [])
    if len(message) > 4000 or not (message or images):
        raise ValueError("message must contain text or images and at most 4000 characters")
    return {
        "message": message,
        "streamingBehavior": streaming_behavior,
        "images": images or None,
    }


def _image_payloads(attachments):
    if not isinstance(attachments, list) or len(attachments) > 4:
        raise ValueError("invalid attachments")
    return validated_image_content([_decode_image(attachment) for attachment in attachments])


def _decode_image(attachment):
    if not isinstance(attachment, dict) or set(attachment) != {"name", "type", "data"}:
        raise ValueError("invalid attachment")
    mime_type = attachment.get("type")
    encoded = attachment.get("data")
    if not _valid_encoded_image(mime_type, encoded):
        raise ValueError("invalid image attachment")
    prefix = f"data:{mime_type};base64,"
    if not encoded.startswith(prefix):
        raise ValueError("invalid image attachment")
    payload = encoded[len(prefix) :]
    return {"type": "image", "data": payload, "mimeType": mime_type}


def _valid_encoded_image(mime_type, encoded):
    return (
        isinstance(mime_type, str)
        and re.fullmatch(r"image/[A-Za-z0-9.+-]{1,64}", mime_type) is not None
        and isinstance(encoded, str)
        and len(encoded) <= 1024 * 1024
    )


def rpc_command_for_route(suffix, data, session_root, request_id=None):
    if suffix == "messages":
        behavior = data.get("streamingBehavior")
        if behavior not in {None, "steer", "followUp"}:
            raise ValueError("invalid streaming behavior")
        return "sendMessage", message_payload(data, streaming_behavior=behavior)
    if suffix == "follow-ups":
        payload = message_payload(data)
        payload.pop("streamingBehavior")
        return "followUp", payload
    return _non_message_command(suffix, data, session_root, request_id)


def _non_message_command(suffix, data, session_root, request_id):
    if suffix in {"interrupt", "shutdown"}:
        return suffix, {}
    if suffix == "model":
        return "setModel", {
            "provider": bounded_text(data.get("provider"), "provider", 200),
            "modelId": bounded_text(data.get("modelId"), "modelId", 200),
        }
    if suffix == "thinking":
        return _thinking_command(data)
    if suffix == "branch":
        return "branch", {"entryId": bounded_text(data.get("entryId"), "entryId", 256)}
    if suffix in {"switch", "resume"}:
        return "switchSession", {"sessionPath": _owned_session_path(data, session_root)}
    if suffix == "compact":
        return _compact_command(data)
    if suffix == "ui-respond" and request_id is not None:
        return "respondToUi", {"requestId": request_id, "value": _ui_value(data)}
    if suffix == "ui-cancel" and request_id is not None:
        return "cancelUi", {"requestId": request_id}
    raise ValueError("invalid session action")


def _thinking_command(data):
    level = data.get("level")
    if level not in REQUEST_THINKING_LEVELS:
        raise ValueError("invalid thinking level")
    return "setThinking", {"level": level}


def _owned_session_path(data, session_root):
    session_path = bounded_text(data.get("sessionPath"), "sessionPath", 4096)
    root = Path(session_root).resolve(strict=True)
    candidate = Path(session_path).expanduser().absolute()
    resolved = candidate.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise ValueError("sessionPath is outside the session root") from error
    if candidate != resolved or not resolved.is_file():
        raise ValueError("sessionPath is not an owned regular session file")
    return str(resolved)


def _compact_command(data):
    instructions = data.get("customInstructions")
    if instructions is not None and (not isinstance(instructions, str) or len(instructions) > 4000):
        raise ValueError("invalid customInstructions")
    return "compact", {"customInstructions": instructions}


def _ui_value(data):
    value = data.get("value")
    if isinstance(value, str):
        return bounded_text(value, "UI response", 16_000)
    if isinstance(value, bool):
        return value
    if (
        not isinstance(value, list)
        or not value
        or len(value) > 128
        or any(not isinstance(item, str) for item in value)
        or sum(len(item) for item in value) > 16_000
    ):
        raise ValueError("UI response must be text, boolean, or selected values")
    return [bounded_text(item, "UI response value", 1_000) for item in value]


def request_host(headers, allowed_hosts):
    value = headers.get("Host", "")
    try:
        parsed = urlparse(f"//{value}")
        _ = parsed.port
    except ValueError:
        return None
    if not _valid_host_value(value, parsed, allowed_hosts):
        return None
    return value


def _valid_host_value(value, parsed, allowed_hosts):
    return (
        bool(value)
        and value.strip() == value
        and "," not in value
        and (
            not any(ord(character) < 33 or ord(character) == 127 for character in value)
            and parsed.username is None
            and parsed.password is None
            and not parsed.path
            and not parsed.params
            and not parsed.query
            and not parsed.fragment
            and parsed.hostname in allowed_hosts
        )
    )


def trusted_browser_request(headers, allowed_hosts):
    host = request_host(headers, allowed_hosts)
    origin = headers.get("Origin")
    if not host or not origin or headers.get("Sec-Fetch-Site") == "cross-site":
        return False
    parsed = urlparse(origin)
    return (
        parsed.scheme in {"http", "https"}
        and parsed.netloc == host
        and parsed.path in {"", "/"}
        and not parsed.params
        and not parsed.query
        and not parsed.fragment
    )


def cookie_credential(headers, name=COOKIE_NAME):
    cookie = SimpleCookie()
    try:
        cookie.load(headers.get("Cookie", ""))
    except Exception:
        return None
    morsel = cookie.get(name)
    return morsel.value if morsel else None
