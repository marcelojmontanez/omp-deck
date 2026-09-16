from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable
from typing import Any


def _remaining(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


def _join_threads(threads: Iterable[threading.Thread], deadline: float) -> bool:
    current = threading.current_thread()
    joined = tuple(thread for thread in threads if thread is not current)
    for thread in joined:
        thread.join(_remaining(deadline))
    return any(thread.is_alive() for thread in joined)


def _call_with_timeout(
    callback: Callable[[], None],
    timeout: float,
    *,
    label: str,
) -> None:
    if timeout <= 0:
        raise RuntimeError(f"{label} timed out")
    done = threading.Event()
    failures: list[Exception] = []

    def run() -> None:
        try:
            callback()
        except Exception as error:
            failures.append(error)
        finally:
            done.set()

    threading.Thread(
        target=run,
        name="omp-deck-runtime-close",
        daemon=True,
    ).start()
    if not done.wait(timeout):
        raise RuntimeError(f"{label} timed out")
    if failures:
        raise failures[0]


def _close_runtimes(
    manager: Any,
    runtimes: Iterable[Any],
    *,
    deadline: float,
    lock_for: Callable[[str], Any],
    failure_reason: str,
    success_reason: str,
) -> list[str]:
    errors = []
    for runtime in runtimes:
        operation_lock = lock_for(runtime.session_id)
        if not operation_lock.acquire(timeout=_remaining(deadline)):
            errors.append(f"{runtime.session_id}: session did not become idle")
            continue
        try:
            if runtime.adapter is not None:
                _call_with_timeout(
                    runtime.adapter.close,
                    _remaining(deadline),
                    label=f"{runtime.session_id} transport close",
                )
        except Exception as error:
            manager.model.set_transport(
                runtime.session_id,
                instance_id=runtime.instance_id,
                generation=runtime.generation,
                status="disconnected",
                reason=f"{failure_reason}: {error}",
            )
            errors.append(f"{runtime.session_id}: {error}")
            continue
        finally:
            operation_lock.release()
        with manager._lock:
            if manager._runtimes.get(runtime.session_id) is runtime:
                manager._runtimes.pop(runtime.session_id)
        manager.model.set_transport(
            runtime.session_id,
            instance_id=None,
            generation=runtime.generation,
            status="disconnected",
            reason=success_reason,
        )
    return errors


def _raise_errors(label: str, errors: list[str]) -> None:
    if errors:
        raise RuntimeError(f"{label} shutdown failed: {'; '.join(errors)}")


def close_rpc_manager(
    manager: Any,
    runtimes: Iterable[Any],
    scheduler: Any,
    timeout: float,
    lock_for: Callable[[str], Any],
) -> None:
    if timeout < 0:
        raise ValueError("timeout must not be negative")
    deadline = time.monotonic() + timeout
    try:
        scheduler.close(timeout=0)
    except RuntimeError:
        pass
    errors = _close_runtimes(
        manager,
        runtimes,
        deadline=deadline,
        lock_for=lock_for,
        failure_reason="close failed",
        success_reason="closed",
    )
    try:
        scheduler.close(timeout=_remaining(deadline))
    except Exception as error:
        errors.append(str(error))
    _raise_errors("RPC session", errors)


def close_terminal_manager(
    manager: Any,
    runtimes: Iterable[Any],
    watcher: threading.Thread | None,
    recovery_workers: Iterable[threading.Thread],
    timeout: float,
    lock_for: Callable[[str], Any],
) -> None:
    if timeout < 0:
        raise ValueError("timeout must not be negative")
    deadline = time.monotonic() + timeout
    errors = []
    if watcher is not None and _join_threads((watcher,), deadline):
        errors.append("extension watcher did not stop")
    errors.extend(
        _close_runtimes(
            manager,
            runtimes,
            deadline=deadline,
            lock_for=lock_for,
            failure_reason="service stop failed",
            success_reason="service stopped",
        )
    )
    if _join_threads(recovery_workers, deadline):
        errors.append("Collab recovery workers did not stop")
    _raise_errors("terminal session", errors)
