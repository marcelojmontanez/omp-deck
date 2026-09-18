"""Normalize public OMP asynchronous job snapshots for fleet projection."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

_MAX_ASYNC_JOBS = 500
_JOB_STATUSES = frozenset({"running", "completed", "failed", "cancelled"})


def _valid_delivery(value: Mapping[object, object]) -> bool:
    queued = value.get("queued")
    delivering = value.get("delivering")
    pending = value.get("pendingJobIds")
    next_retry = value.get("nextRetryAt")
    return (
        isinstance(queued, int)
        and not isinstance(queued, bool)
        and queued >= 0
        and isinstance(delivering, bool)
        and isinstance(pending, Sequence)
        and not isinstance(pending, (str, bytes, bytearray))
        and len(pending) <= _MAX_ASYNC_JOBS
        and all(isinstance(job_id, str) and 0 < len(job_id) <= 4096 for job_id in pending)
        and (
            next_retry is None
            or isinstance(next_retry, (int, float))
            and not isinstance(next_retry, bool)
            and math.isfinite(next_retry)
        )
    )


def normalize_async_jobs(value: object) -> list[dict[str, Any]]:
    """Return bounded task-job rows from the public ``AsyncJobSnapshot`` envelope."""
    if not isinstance(value, Mapping):
        return []
    running = value.get("running")
    recent = value.get("recent")
    delivery = value.get("delivery")
    if (
        not isinstance(running, Sequence)
        or isinstance(running, (str, bytes, bytearray))
        or not isinstance(recent, Sequence)
        or isinstance(recent, (str, bytes, bytearray))
        or not isinstance(delivery, Mapping)
        or not _valid_delivery(delivery)
    ):
        return []

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for collection in (running, recent):
        for value in collection:
            if not isinstance(value, Mapping) or value.get("type") != "task":
                continue
            job_id = value.get("id")
            status = value.get("status")
            label = value.get("label")
            start_time = value.get("startTime")
            agent_id = value.get("agentId")
            if (
                not isinstance(job_id, str)
                or not job_id
                or len(job_id) > 4096
                or job_id in seen
                or status not in _JOB_STATUSES
                or not isinstance(label, str)
                or not label
                or len(label) > 4096
                or isinstance(start_time, bool)
                or not isinstance(start_time, (int, float))
                or not math.isfinite(start_time)
                or (agent_id is not None and (not isinstance(agent_id, str) or not agent_id or len(agent_id) > 4096))
            ):
                continue
            row: dict[str, Any] = {
                "id": job_id,
                "type": "task",
                "status": status,
                "label": label,
                "startTime": start_time,
            }
            if agent_id is not None:
                row["agentId"] = agent_id
            normalized.append(row)
            seen.add(job_id)
            if len(normalized) == _MAX_ASYNC_JOBS:
                return normalized
    return normalized
