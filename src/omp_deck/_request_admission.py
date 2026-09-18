from __future__ import annotations

import os
import re
import threading
from pathlib import Path
from typing import Any

from ._private_state import PrivateStateFile, validate_private_directory, validate_private_file
from ._request_ledger_schema import RequestLedgerError

_LEASE_NAME = re.compile(r"([a-f0-9]{64})\.lock")
_LOCAL_LOCK = threading.Lock()
_LOCAL_LEASES: set[str] = set()


class RequestAdmissions:
    """OS-owned live admissions; every operation runs under the ledger lock."""

    def __init__(self, ledger_path: Path) -> None:
        self.root = ledger_path.with_name(f".{ledger_path.name}.admissions")

    def _storage(self, token: str) -> PrivateStateFile:
        if re.fullmatch(r"[a-f0-9]{64}", token) is None:
            raise RequestLedgerError("invalid admission identity")
        return PrivateStateFile(self.root / token, lock_name=f"{token}.lock")

    def _key(self, token: str) -> str:
        return os.path.normcase(str((self.root / token).resolve()))

    def claim(self, token: str) -> Any | None:
        key = self._key(token)
        # Windows named mutexes are recursive on their owning thread. Local
        # ownership must still make a nested probe report this admission as live.
        with _LOCAL_LOCK:
            if key in _LOCAL_LEASES:
                return None
        context = self._storage(token).locked(blocking=False)
        if context.__enter__() is None:
            context.__exit__(None, None, None)
            return None
        with _LOCAL_LOCK:
            _LOCAL_LEASES.add(key)
        return context

    def release(self, token: str, context: Any) -> None:
        try:
            context.__exit__(None, None, None)
        finally:
            with _LOCAL_LOCK:
                _LOCAL_LEASES.discard(self._key(token))
        self._delete(token)

    def active(self, token: str) -> bool:
        with _LOCAL_LOCK:
            if self._key(token) in _LOCAL_LEASES:
                return True
        path = self.root / f"{token}.lock"
        if not path.exists() and not path.is_symlink():
            return False
        with self._storage(token).locked(blocking=False) as acquired:
            return acquired is None

    def validate_directory(self) -> int:
        return sum(1 for _ in self._entries())

    def sweep(self) -> None:
        for token in self._entries():
            if token is not None and not self.active(token):
                self._delete(token)

    def _entries(self):
        if not self.root.exists() and not self.root.is_symlink():
            return
        validate_private_directory(self.root)
        with os.scandir(self.root) as entries:
            for index, entry in enumerate(entries):
                if index >= 4096:
                    raise RequestLedgerError("request admission store has too many entries")
                validate_private_file(Path(entry.path), 0)
                if entry.name == ".cleanup.lock":
                    yield None
                    continue
                match = _LEASE_NAME.fullmatch(entry.name)
                if match is None:
                    raise RequestLedgerError("request admission store contains an unknown entry")
                yield match[1]

    def _delete(self, token: str) -> None:
        storage = PrivateStateFile(self.root / f"{token}.lock", lock_name=".cleanup.lock")
        with storage.locked():
            storage.delete()
