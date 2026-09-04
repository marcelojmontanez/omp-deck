import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from omp_rpc import (
    ReadyEvent,
    RpcClient,
    RpcError,
    RpcProcessExitError,
    RpcTimeoutError,
)


FIXTURE = Path(__file__).parent / "fixtures" / "fake_rpc_server.py"


class RpcCompatibilityTests(unittest.TestCase):
    def client(self, scenario, transcript=None, **kwargs):
        command = [sys.executable, "-u", str(FIXTURE), scenario]
        if transcript is not None:
            command.append(str(transcript))
        return RpcClient(
            command=command,
            startup_timeout=kwargs.pop("startup_timeout", 1.0),
            request_timeout=kwargs.pop("request_timeout", 1.0),
            **kwargs,
        )

    def transcript(self, path):
        return [json.loads(line) for line in path.read_text().splitlines()]

    def test_ready_advertisement_negotiates_v2_with_exact_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "requests.jsonl"
            ready_events = []
            client = self.client("contract", transcript)
            client.on_ready(ready_events.append)
            with client:
                state = client.get_state()

            self.assertEqual(state.session_id, "fixture-session")
            self.assertEqual(len(ready_events), 1)
            self.assertIsInstance(ready_events[0], ReadyEvent)
            self.assertEqual(ready_events[0].protocol_version, 1)
            self.assertEqual(ready_events[0].supported_protocol_versions, (1, 2))
            self.assertEqual(ready_events[0].max_frame_bytes, 1024 * 1024)
            self.assertEqual(ready_events[0].max_reassembled_frame_bytes, 64 * 1024 * 1024)
            self.assertEqual(
                self.transcript(transcript)[0],
                {"id": "req_1", "type": "negotiate_protocol", "protocolVersion": 2},
            )

    def test_v1_ready_falls_back_without_negotiation(self):
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "requests.jsonl"
            with self.client("v1", transcript) as client:
                state = client.get_state()

            self.assertEqual(state.session_id, "v1-session")
            requests = self.transcript(transcript)
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0]["type"], "get_state")
            self.assertEqual(requests[0]["id"], "req_1")

    def test_correlates_out_of_order_responses_by_request_id(self):
        with self.client("out_of_order") as client:
            results = {}
            errors = []

            def get_state():
                try:
                    results["state"] = client.get_state().session_id
                except BaseException as error:
                    errors.append(error)

            def get_messages():
                try:
                    results["messages"] = client.get_messages()
                except BaseException as error:
                    errors.append(error)

            threads = [threading.Thread(target=get_state), threading.Thread(target=get_messages)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(2)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(results["state"], "correlated")
        self.assertEqual(results["messages"][0]["content"], "correlated")

    def test_prompt_follow_up_state_messages_and_ui_payloads(self):
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "requests.jsonl"
            image = {"type": "image", "data": "aW1hZ2U=", "mimeType": "image/png"}
            with self.client("contract", transcript) as client:
                first_ui = client.next_ui_request(timeout=1)
                second_ui = client.next_ui_request(timeout=1)
                client.send_ui_value(first_ui.id, "typed answer")
                client.cancel_ui_request(second_ui.id, timed_out=True)
                client.prompt("start", images=[image], streaming_behavior="followUp")
                client.follow_up("continue", images=[image])
                state = client.get_state()
                messages = client.get_messages()

            self.assertEqual((first_ui.method, first_ui.title), ("input", "Question"))
            self.assertEqual((second_ui.method, second_ui.message), ("confirm", "Proceed?"))
            self.assertEqual((state.session_id, state.session_name, state.message_count), ("fixture-session", "Fixture", 2))
            self.assertEqual(messages, ({"role": "user", "content": "hello"},))
            requests = self.transcript(transcript)
            by_type = {request["type"]: request for request in requests if request["type"] != "extension_ui_response"}
            self.assertEqual(
                by_type["prompt"],
                {"id": by_type["prompt"]["id"], "type": "prompt", "message": "start", "images": [image], "streamingBehavior": "followUp"},
            )
            self.assertEqual(
                by_type["follow_up"],
                {"id": by_type["follow_up"]["id"], "type": "follow_up", "message": "continue", "images": [image]},
            )
            ui_responses = [request for request in requests if request["type"] == "extension_ui_response"]
            self.assertEqual(ui_responses, [
                {"type": "extension_ui_response", "id": "ui-input", "value": "typed answer"},
                {"type": "extension_ui_response", "id": "ui-confirm", "cancelled": True, "timedOut": True},
            ])
            self.assertEqual(by_type["get_messages_page"]["limit"], 256)

    def test_accepts_valid_v2_chunks_within_reassembly_bound(self):
        with self.client("valid_chunk", request_timeout=2.0) as client:
            state = client.get_state()

        self.assertEqual(state.session_id, "chunked")
        self.assertEqual(len(state.session_name), 1024 * 1024)

    def test_rejects_malformed_chunk_data(self):
        with self.client("malformed_chunk", request_timeout=2.0) as client:
            with self.assertRaisesRegex(RpcError, "chunk|base64"):
                client.get_state()

    def test_rejects_chunk_exceeding_reassembly_bound(self):
        with self.client("oversized_chunk", request_timeout=2.0) as client:
            with self.assertRaisesRegex(RpcError, "chunk"):
                client.get_state()

    def test_eof_fails_a_pending_request(self):
        with self.client("eof") as client:
            with self.assertRaises(RpcProcessExitError):
                client.get_state()

    def test_request_and_startup_timeouts_are_bounded(self):
        started = time.monotonic()
        with self.client("timeout", request_timeout=0.1) as client:
            with self.assertRaisesRegex(RpcTimeoutError, "get_state"):
                client.get_state()
        self.assertLess(time.monotonic() - started, 2.0)

        started = time.monotonic()
        with self.assertRaisesRegex(RpcTimeoutError, "ready"):
            self.client("startup_timeout", startup_timeout=0.1).start()
        self.assertLess(time.monotonic() - started, 2.0)

    def test_context_manager_reaps_the_fake_process(self):
        client = self.client("cleanup")
        with client:
            process = client._process
            self.assertIsNotNone(process)
            self.assertIsNone(process.poll())

        self.assertIsNotNone(process.poll())
        self.assertIsNone(client._process)


if __name__ == "__main__":
    unittest.main()
