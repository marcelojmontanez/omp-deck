from __future__ import annotations

import os
import sys
from pathlib import Path

from omp_rpc import RpcClient


def create_rpc_client(
    *,
    executable: str,
    session_root: Path,
    project_path: str,
    resume_file: str | None,
    provider: str | None = None,
    model: str | None = None,
    thinking: str | None = None,
) -> RpcClient:
    options = {
        "cwd": project_path,
        "max_event_history": 512,
        "max_stderr_chunks": 128,
    }
    client = RpcClient(
        executable=executable,
        provider=provider,
        model=model,
        thinking=thinking,
        session_dir=session_root,
        extra_args=("--resume", resume_file) if resume_file else (),
        **options,
    )
    command = getattr(client, "command", None)
    if os.name != "nt" or not isinstance(command, tuple):
        return client
    return RpcClient(
        command=(sys.executable, "-m", "omp_deck._windows_job", *command),
        **options,
    )
