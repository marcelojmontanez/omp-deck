from __future__ import annotations

import threading
import time
import unittest

from omp_deck._collab_recovery import CollabRecoverySupervisor
from omp_deck._rpc_errors import AdapterIncompatibleError


class Credentials:
    def __init__(self) -> None:
        self.link = "synthetic-collab-link"

    def get_link(self, session_id: str) -> str | None:
        return self.link if session_id == "collab-1" else None


class Model:
    def __init__(self) -> None:
        self.disconnected = threading.Event()
        self.updates: list[dict[str, object]] = []

    def set_transport(self, session_id: str, **update: object) -> None:
        self.updates.append({"sessionId": session_id, **update})
        self.disconnected.set()


class CollabRecoveryTests(unittest.TestCase):
    def test_transient_startup_outage_retries_without_service_restart(self) -> None:
        stop = threading.Event()
        attempts: list[str] = []
        connected = threading.Event()

        def connect(record, link: str) -> None:
            attempts.append(link)
            if len(attempts) == 1:
                raise OSError("relay unavailable")
            connected.set()

        supervisor = CollabRecoverySupervisor(
            stop,
            Credentials(),
            Model(),
            connect,
            retry_initial=0.01,
            retry_max=0.02,
        )
        supervisor.start([{"id": "collab-1", "generation": 1}])
        try:
            self.assertTrue(connected.wait(1))
            self.assertEqual(attempts, ["synthetic-collab-link", "synthetic-collab-link"])
        finally:
            stop.set()
            workers = supervisor.stop()
            for worker in workers:
                worker.join(1)

    def test_stop_cancels_scheduled_retry(self) -> None:
        stop = threading.Event()
        attempted = threading.Event()
        attempts = 0

        def connect(record, link: str) -> None:
            nonlocal attempts
            attempts += 1
            attempted.set()
            raise OSError("relay unavailable")

        supervisor = CollabRecoverySupervisor(
            stop,
            Credentials(),
            Model(),
            connect,
            retry_initial=0.1,
            retry_max=0.1,
        )
        supervisor.start([{"id": "collab-1", "generation": 1}])
        self.assertTrue(attempted.wait(1))
        stop.set()
        workers = supervisor.stop()
        for worker in workers:
            worker.join(1)
        time.sleep(0.12)
        self.assertEqual(attempts, 1)

    def test_protocol_failure_is_terminal_and_not_retried(self) -> None:
        stop = threading.Event()
        model = Model()
        attempts = 0

        def connect(record, link: str) -> None:
            nonlocal attempts
            attempts += 1
            raise AdapterIncompatibleError("unsupported host", kind="protocol")

        supervisor = CollabRecoverySupervisor(
            stop,
            Credentials(),
            model,
            connect,
            retry_initial=0.01,
            retry_max=0.02,
        )
        supervisor.start([{"id": "collab-1", "generation": 1}])
        try:
            self.assertTrue(model.disconnected.wait(1))
            time.sleep(0.05)
            self.assertEqual(attempts, 1)
        finally:
            stop.set()
            workers = supervisor.stop()
            for worker in workers:
                worker.join(1)


if __name__ == "__main__":
    unittest.main()
