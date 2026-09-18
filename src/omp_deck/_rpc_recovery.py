from __future__ import annotations

import dataclasses
import json
import os
import stat
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, TextIO

from ._rpc_errors import AdapterIncompatibleError
from .projects import resolve_project_path

_MAX_HEADER_LINE_CHARS = 64 * 1024


@dataclasses.dataclass(slots=True)
class _Recovery:
    generation: int
    cancelled: threading.Event
    thread: threading.Thread | None = None


class RpcRecoverySupervisor:
    """Own bounded-backoff retries for desired RPC sessions without a runtime."""

    def __init__(
        self,
        retry: Callable[[str, int], int | None],
        *,
        initial_delay: float = 0.1,
        maximum_delay: float = 2.0,
    ) -> None:
        if initial_delay < 0 or maximum_delay < initial_delay:
            raise ValueError("invalid RPC recovery backoff")
        self._retry = retry
        self._initial_delay = initial_delay
        self._maximum_delay = maximum_delay
        self._lock = threading.Lock()
        self._recoveries: dict[str, _Recovery] = {}
        self._closed = False

    def request(self, session_id: str, generation: int) -> None:
        with self._lock:
            if self._closed:
                return
            current = self._recoveries.get(session_id)
            if current is not None:
                current.generation = max(current.generation, generation)
                return
            recovery = _Recovery(generation, threading.Event())
            thread = threading.Thread(
                target=self._run,
                args=(session_id, recovery),
                name=f"omp-deck-rpc-recovery-{session_id[:16]}",
                daemon=True,
            )
            recovery.thread = thread
            self._recoveries[session_id] = recovery
            thread.start()

    def cancel(self, session_id: str) -> None:
        with self._lock:
            recovery = self._recoveries.get(session_id)
            if recovery is not None:
                recovery.cancelled.set()

    def close(self, timeout: float = 5.0) -> None:
        if timeout < 0:
            raise ValueError("timeout must not be negative")
        with self._lock:
            self._closed = True
            recoveries = tuple(self._recoveries.values())
            for recovery in recoveries:
                recovery.cancelled.set()
        deadline = time.monotonic() + timeout
        current = threading.current_thread()
        for recovery in recoveries:
            thread = recovery.thread
            if thread is not None and thread is not current:
                thread.join(max(0.0, deadline - time.monotonic()))
        if any(
            recovery.thread is not None and recovery.thread is not current and recovery.thread.is_alive()
            for recovery in recoveries
        ):
            raise RuntimeError("RPC recovery workers did not stop")

    def _run(self, session_id: str, recovery: _Recovery) -> None:
        delay = self._initial_delay
        try:
            while not recovery.cancelled.wait(delay):
                with self._lock:
                    if self._closed or self._recoveries.get(session_id) is not recovery:
                        return
                    generation = recovery.generation
                try:
                    next_generation = self._retry(session_id, generation)
                except BaseException:
                    next_generation = generation
                if next_generation is None:
                    return
                with self._lock:
                    if self._recoveries.get(session_id) is not recovery:
                        return
                    recovery.generation = next_generation
                delay = min(self._maximum_delay, max(self._initial_delay, delay * 2))
        finally:
            with self._lock:
                if self._recoveries.get(session_id) is recovery:
                    self._recoveries.pop(session_id, None)


def retry_disconnected(manager: Any, session_id: str, expected_generation: int) -> int | None:
    """Try one generation-fenced reconnect; return the next retry generation."""
    with manager._operation_lock(session_id):
        with manager._lock:
            if manager._closed or session_id in manager._shutdown_requested:
                return None
        try:
            record = manager._record(session_id)
        except KeyError:
            return None
        if (
            record["adapterKind"] != "rpc"
            or not record["desiredRpcRunning"]
            or record["generation"] != expected_generation
            or manager._runtime_or_none(session_id) is not None
        ):
            return None
        try:
            manager._connect(record)
        except Exception as error:
            return record_recovery_failure(manager, session_id, error, prefix="recovery failed")
        return None


def record_recovery_failure(manager: Any, session_id: str, error: Exception, *, prefix: str) -> int | None:
    try:
        current = manager._record(session_id)
    except KeyError:
        return None
    with manager._lock:
        if (
            manager._closed
            or session_id in manager._shutdown_requested
            or current["adapterKind"] != "rpc"
            or not current["desiredRpcRunning"]
            or manager._runtime_or_none(session_id) is not None
        ):
            return None
        manager.model.set_transport(
            session_id,
            instance_id=None,
            generation=current["generation"],
            status="disconnected",
            reason=f"{prefix}: {error}",
        )
        if isinstance(error, (AdapterIncompatibleError, ValueError)):
            return None
        manager._recoveries.request(session_id, current["generation"])
        return current["generation"]


def recovery_session_file(session_root: Path, record: Mapping[str, Any]) -> str | None:
    omp_session_id = record.get("ompSessionId")
    if omp_session_id is None:
        return None
    recovery = record.get("recoveryMetadata")
    session_file = recovery.get("sessionFile") if isinstance(recovery, Mapping) else None
    return validated_session_file(session_root, session_file, omp_session_id)


def validate_recovered_identity(
    session_root: Path,
    record: Mapping[str, Any],
    state: Mapping[str, Any],
    resume_file: str,
) -> None:
    if state.get("session_id") != record.get("ompSessionId"):
        raise ValueError("OMP resumed a different session identity")
    active_file = validated_session_file(session_root, state.get("session_file"), state.get("session_id"))
    if active_file != resume_file:
        raise ValueError("OMP resumed a different session file")


def validated_session_file(session_root: Path, session_file: object, expected_session_id: object) -> str:
    """Return a canonical, rooted OMP session file with the expected header identity."""
    if not isinstance(session_file, str) or not session_file:
        raise ValueError("RPC session state does not include a session file")
    if not isinstance(expected_session_id, str) or not expected_session_id:
        raise ValueError("RPC session state does not include a session ID")

    candidate = Path(session_file)
    if not candidate.is_absolute():
        raise ValueError("RPC session file must be an absolute path")
    try:
        root = session_root.resolve(strict=True)
    except OSError as error:
        raise ValueError("RPC session root is missing or inaccessible") from error
    if not root.is_dir():
        raise ValueError("RPC session root is not a directory")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise ValueError("registered RPC session file is missing or inaccessible") from error
    try:
        resolved.relative_to(root)
    except ValueError:
        raise ValueError("registered RPC session file is outside the configured session root") from None
    if candidate != resolved:
        raise ValueError("registered RPC session file must not use filesystem aliases")
    if resolved.suffix != ".jsonl" or not resolved.is_file():
        raise ValueError("registered RPC session file is not an OMP session file")

    header = _read_session_header(resolved)
    actual_session_id = header.get("id")
    if actual_session_id != expected_session_id:
        raise ValueError("registered RPC session file does not match its OMP session ID")
    return str(resolved)


def persist_rpc_identity(
    registry: Any,
    session_root: Path,
    record: Mapping[str, Any],
    snapshot: Any,
) -> dict[str, Any]:
    state = snapshot.state
    session_file = state.get("session_file")
    omp_session_id = state.get("session_id")
    recovery = record.get("recoveryMetadata")
    if record.get("ompSessionId") == omp_session_id and recovery == {"sessionFile": session_file}:
        return dict(record)
    canonical_file = validated_session_file(session_root, session_file, omp_session_id)
    next_recovery = {"sessionFile": canonical_file}
    if record.get("ompSessionId") == omp_session_id and recovery == next_recovery:
        return dict(record)
    return registry.update(
        record["id"],
        omp_session_id=omp_session_id,
        recovery_metadata=next_recovery,
    )


def validated_project(project_root: Path, record: Mapping[str, Any]) -> str:
    project = record["project"]
    resolved = resolve_project_path(project_root, project["name"], must_exist=True)
    if str(resolved) != project["path"] or not resolved.is_dir():
        raise ValueError("registered project path is no longer valid")
    return str(resolved)


def replace_disconnected(
    manager: Any,
    session_id: str,
    error: Exception,
    *,
    expected_runtime: Any | None = None,
) -> None:
    with manager._operation_lock(session_id):
        runtime = manager._runtime_or_none(session_id)
        if runtime is None or (expected_runtime is not None and runtime is not expected_runtime):
            return
        if runtime.supervisor is not None:
            runtime.supervisor.request_stop()
        if runtime.adapter is not None:
            try:
                runtime.adapter.close()
            except Exception as close_error:
                manager.model.set_transport(
                    session_id,
                    instance_id=runtime.instance_id,
                    generation=runtime.generation,
                    status="disconnected",
                    reason=f"{error}; close failed: {close_error}",
                )
                return
        if runtime.supervisor is not None:
            runtime.supervisor.join(1.0)
        with manager._lock:
            if manager._runtimes.get(session_id) is runtime:
                manager._runtimes.pop(session_id)
        manager.model.set_transport(
            session_id,
            instance_id=runtime.instance_id,
            generation=runtime.generation,
            status="disconnected",
            reason=str(error),
        )
        try:
            record = manager._record(session_id)
        except KeyError:
            return
        if not record["desiredRpcRunning"]:
            return
        with manager._lock:
            if manager._closed:
                return
        try:
            manager._connect(record)
        except Exception as reconnect_error:
            record_recovery_failure(manager, session_id, reconnect_error, prefix="reconnect failed")


def _read_session_header(session_file: Path) -> dict[str, Any]:
    descriptor = None
    try:
        descriptor = os.open(session_file, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        info = os.fstat(descriptor)
        current = session_file.stat(follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
            raise ValueError("session file changed while it was opened")
        with os.fdopen(descriptor, "r", encoding="utf-8") as source:
            descriptor = None
            first = _read_record(source)
            header = _read_record(source) if first.get("type") == "title" else first
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("registered RPC session file has a missing or malformed header") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if header.get("type") != "session" or not isinstance(header.get("id"), str) or not header["id"]:
        raise ValueError("registered RPC session file has a missing or malformed header")
    return header


def _read_record(source: TextIO) -> dict[str, Any]:
    line = source.readline(_MAX_HEADER_LINE_CHARS + 1)
    if not line or (len(line) > _MAX_HEADER_LINE_CHARS and not line.endswith("\n")):
        raise ValueError("missing or oversized session header")
    value = json.loads(line)
    if not isinstance(value, dict):
        raise ValueError("session header is not an object")
    return value
