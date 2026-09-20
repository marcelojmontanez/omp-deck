from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .rpc_adapter import SessionAdapter
from .rpc_requests import FreshSessionMismatchError


def persist_fresh_settings(
    adapter: SessionAdapter, provider: str | None, model: str | None, thinking: str | None
) -> None:
    if provider is None or model is None:
        return
    # Startup flags alone do not persist the model in empty OMP 18.2.6 sessions.
    adapter.set_model(provider, model)
    if thinking is not None:
        adapter.set_thinking(thinking)


def launch_fresh_session(
    manager: Any,
    project_name: str,
    project_path: Path,
    *,
    provider: str,
    model: str,
    thinking: str,
    source_session_id: str,
    source_omp_session_id: str,
) -> dict[str, Any]:
    session = manager.launch(
        project_name,
        project_path,
        provider=provider,
        model=model,
        thinking=thinking,
    )
    if _matches_fresh_contract(
        session,
        provider=provider,
        model=model,
        thinking=thinking,
        source_session_id=source_session_id,
        source_omp_session_id=source_omp_session_id,
    ):
        return session

    session_id = session["id"]
    cleanup_error = None
    try:
        manager.shutdown(session_id)
    except Exception as error:
        cleanup_error = error
    if cleanup_error is None:
        manager.registry.remove(session_id)
        manager.model.remove(session_id)
        raise FreshSessionMismatchError("fresh RPC session did not preserve the source settings")
    raise RuntimeError(f"fresh RPC session settings mismatch; cleanup failed: {cleanup_error}") from cleanup_error


def _matches_fresh_contract(
    session: Mapping[str, Any],
    *,
    provider: str,
    model: str,
    thinking: str,
    source_session_id: str,
    source_omp_session_id: str,
) -> bool:
    actual_model = session.get("model")
    return (
        session.get("id") != source_session_id
        and isinstance(session.get("ompSessionId"), str)
        and bool(session["ompSessionId"])
        and session["ompSessionId"] != source_omp_session_id
        and isinstance(actual_model, Mapping)
        and actual_model.get("provider") == provider
        and actual_model.get("id") == model
        and session.get("thinking") == thinking
        and session.get("messages") == []
        and session.get("streamingMessage") is None
    )
