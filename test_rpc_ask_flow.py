import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

from omp_rpc import RpcClient

from omp_deck.rpc_adapter import SessionAdapter


FIXTURE = Path(__file__).parent / "test_fixtures" / "fake_rpc_server.py"


class RpcAskFlowTests(unittest.TestCase):
    def adapter(self, transcript):
        return SessionAdapter(
            lambda: RpcClient(
                command=(sys.executable, str(FIXTURE), "ask_flow", str(transcript)),
                startup_timeout=2,
                request_timeout=2,
                max_event_history=32,
                max_stderr_chunks=16,
            )
        )

    def pending(self, adapter, expected_id):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            pending = adapter.pending_ui_request()
            if pending is not None and pending.id == expected_id:
                return pending
            time.sleep(0.01)
        self.fail(f"pending request {expected_id} was not projected")

    def wait_for_responses(self, transcript, count):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if transcript.exists():
                requests = [json.loads(line) for line in transcript.read_text().splitlines()]
                if sum(value["type"] == "extension_ui_response" for value in requests) >= count:
                    return
            time.sleep(0.01)
        self.fail("only partial UI responses reached the RPC peer")

    def test_pending_ask_reappears_after_reconnect_and_completes_in_order(self):
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "requests.jsonl"
            first = self.adapter(transcript)
            first.start()
            initial = self.pending(first, "ask-input")
            self.assertEqual(initial.prefill, "stage-")
            first.close()

            reconnected = self.adapter(transcript)
            reconnected.start()
            replayed = self.pending(reconnected, "ask-input")
            self.assertEqual(replayed.message, "Name the target")
            self.assertEqual(reconnected.respond_ui(replayed.id, "stage-api"), "accepted")

            select = self.pending(reconnected, "ask-select")
            self.assertEqual(
                [(option.value, option.label, option.description) for option in select.options],
                [
                    ("iad", "Virginia", "US East"),
                    ("fra", "Frankfurt", "EU Central"),
                ],
            )
            self.assertEqual(reconnected.respond_ui(select.id, "fra"), "accepted")

            confirm = self.pending(reconnected, "ask-confirm")
            self.assertIsNotNone(confirm.expires_at)
            self.assertEqual(reconnected.respond_ui(confirm.id, True), "accepted")
            self.assertEqual(reconnected.respond_ui(confirm.id, False), "stale")
            self.assertIsNone(reconnected.pending_ui_request())
            self.wait_for_responses(transcript, 3)
            reconnected.close()

            requests = [json.loads(line) for line in transcript.read_text().splitlines()]
            responses = [value for value in requests if value["type"] == "extension_ui_response"]
            self.assertEqual(
                responses,
                [
                    {"type": "extension_ui_response", "id": "ask-input", "value": "stage-api"},
                    {"type": "extension_ui_response", "id": "ask-select", "value": "fra"},
                    {"type": "extension_ui_response", "id": "ask-confirm", "confirmed": True},
                ],
            )


if __name__ == "__main__":
    unittest.main()
