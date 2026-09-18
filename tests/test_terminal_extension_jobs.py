from __future__ import annotations

import unittest

from omp_deck._extension_jobs import normalize_async_jobs
from omp_deck._fleet_extension import project_extension_event


def job(job_id: str, status: str, *, job_type: str = "task") -> dict[str, object]:
    return {
        "id": job_id,
        "type": job_type,
        "status": status,
        "label": f"Worker {job_id}",
        "startTime": 1,
        "agentId": f"agent-{job_id}",
    }


def snapshot(*, running=(), recent=()) -> dict[str, object]:
    return {
        "running": list(running),
        "recent": list(recent),
        "delivery": {"queued": 0, "delivering": False, "pendingJobIds": []},
    }


class ExtensionJobTests(unittest.TestCase):
    def test_official_snapshot_normalizes_task_jobs_and_bounds_retention(self):
        running = [job(str(index), "running") for index in range(600)]
        running.insert(0, job("shell", "running", job_type="bash"))

        normalized = normalize_async_jobs(snapshot(running=running, recent=[job("done", "completed")]))

        self.assertEqual(len(normalized), 500)
        self.assertEqual(normalized[0]["id"], "0")
        self.assertEqual(normalized[-1]["id"], "499")
        self.assertNotIn("shell", {item["id"] for item in normalized})

    def test_live_async_and_agent_end_events_replace_subagent_state(self):
        session = {
            "subagents": [],
            "state": "working",
            "pendingUiRequest": None,
            "workingOn": "Waiting",
        }
        project_extension_event(
            session,
            "terminal-1",
            "async",
            {"snapshot": snapshot(running=[job("worker", "running")])},
        )
        self.assertEqual([(item["id"], item["status"]) for item in session["subagents"]], [("worker", "running")])

        project_extension_event(
            session,
            "terminal-1",
            "lifecycle",
            {
                "state": "agent_end",
                "snapshot": {
                    "idle": True,
                    "pendingMessages": False,
                    "model": None,
                    "async": snapshot(recent=[job("worker", "completed")]),
                },
            },
        )
        self.assertEqual(
            [(item["id"], item["status"]) for item in session["subagents"]],
            [("worker", "completed")],
        )
        self.assertEqual(session["state"], "idle")
        self.assertIsNone(session["workingOn"])


if __name__ == "__main__":
    unittest.main()
