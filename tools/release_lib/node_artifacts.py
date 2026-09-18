from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path


def inspect_wheel(path: Path) -> tuple[str, str, str]:
    from .node_common import ReleaseError, read_file

    raw = read_file(path)
    wheel_hash = hashlib.sha256(raw).hexdigest()
    plugin_hash = hashlib.sha256()
    try:
        with zipfile.ZipFile(path) as archive:
            metadata = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
            if len(metadata) != 1:
                raise ReleaseError("ambiguous wheel metadata")
            fields = {
                key.casefold(): value.strip()
                for line in archive.read(metadata[0]).decode().splitlines()
                if ":" in line
                for key, value in [line.split(":", 1)]
            }
            if fields.get("name", "").casefold().replace("_", "-") != "omp-deck":
                raise ReleaseError("wheel distribution is not omp-deck")
            names = sorted(
                name for name in archive.namelist() if name.startswith("omp_deck/plugin/") and not name.endswith("/")
            )
            for name in names:
                relative = name[len("omp_deck/plugin/") :]
                if relative and relative.split("/")[0] != "node_modules":
                    plugin_hash.update(relative.encode() + b"\0" + archive.read(name) + b"\0")
    except (zipfile.BadZipFile, UnicodeDecodeError, KeyError) as error:
        raise ReleaseError("invalid wheel") from error
    return wheel_hash, fields.get("version", ""), plugin_hash.hexdigest()
