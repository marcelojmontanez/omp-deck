import base64
import binascii
import uuid
from pathlib import Path
from settings import SETTINGS

UPLOAD_ROOT = SETTINGS.upload_root
BLOB_ROOT = Path.home() / ".omp/agent/blobs"


def blob_path(value):
    prefix = "blob:sha256:"
    digest = value.removeprefix(prefix) if isinstance(value, str) else ""
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        return None
    matches = list(BLOB_ROOT.glob(f"{digest}.*")) or list(BLOB_ROOT.glob(digest))
    return matches[0] if matches else None

def message_images(message):
    content = message.get("content", [])
    items = content if isinstance(content, list) else []
    urls = []
    for item in items:
        path = blob_path(item.get("data")) if item.get("type") == "image" else None
        if path:
            url = f"/api/blob/{path.stem}"
            if url not in urls:
                urls.append(url)
    return urls

def cleanup_paths(paths):
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def save_attachments(items):
    if len(items) > 4:
        raise ValueError("at most 4 files may be attached")

    decoded = []
    total = 0
    for item in items:
        encoded = item.get("data", "")
        if not isinstance(encoded, str):
            raise ValueError("invalid file data")
        if "," in encoded:
            encoded = encoded.split(",", 1)[1]
        try:
            content = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("invalid file data") from None
        total += len(content)
        if not content or total > 12 * 1024 * 1024:
            raise ValueError("files exceed the 12 MB limit")
        decoded.append((Path(item.get("name", "")).suffix[:16], content))

    UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    paths = []
    try:
        for suffix, content in decoded:
            path = UPLOAD_ROOT / f"{uuid.uuid4().hex}{suffix}"
            paths.append(path)
            path.write_bytes(content)
    except OSError:
        cleanup_paths(paths)
        raise
    return paths


