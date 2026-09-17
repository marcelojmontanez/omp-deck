"""Real Deck HTTP/SSE boundaries with deterministic in-memory OMP snapshots."""

import argparse
import base64
import hashlib
import json
import signal

from omp_deck import attachments, server
from omp_deck.request_ledger import RequestLedger
from omp_deck.rpc_adapter import AdapterSnapshot

SESSION_ID = "same-local-session"


class BrowserSession:
    def __init__(self, label):
        self.label = label
        self.sequence = 0
        self.thinking = "medium"
        self.messages = [{"role": "assistant", "content": f"{label} reply"}]
        self.requests = RequestLedger(server.SETTINGS.state_root / "requests.json")
        blob = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aM1sAAAAASUVORK5CYII="
        )
        digest = hashlib.sha256(blob).hexdigest()
        root = server.SETTINGS.state_root / "blobs"
        root.mkdir(mode=0o700, exist_ok=True)
        path = root / digest
        path.write_bytes(blob)
        path.chmod(0o600)
        attachments.BLOB_ROOT = root
        self.messages.append({"role": "assistant", "content": [{"type": "image", "data": f"blob:sha256:{digest}"}]})
        registry = server.registry_store()
        record = next((item for item in registry.load() if item["id"] == SESSION_ID), None)
        if record is None:
            record = registry.create(
                session_id=SESSION_ID,
                adapter_kind="rpc",
                project_name="demo",
                project_path=str(server.SETTINGS.project_root / "demo"),
                title="Shared project",
                omp_session_id="same-omp-session",
            )
        self.generation = record["generation"]
        server._fleet_model.load_record(record)
        self.publish()

    def publish(self):
        self.sequence += 1
        server._fleet_model.apply_rpc_snapshot(
            SESSION_ID,
            instance_id="browser-fixture",
            generation=self.generation,
            snapshot=AdapterSnapshot(
                state={"is_streaming": False, "thinking_level": self.thinking},
                messages=tuple(self.messages),
                todos=(),
                subagents=(),
                sequence=self.sequence,
                pending_ui_request=None,
                transport_status="connected",
                reason=None,
            ),
        )

    def request_refresh_connected(self):
        # The fixture publishes synchronously; no external OMP process to refresh.
        return None

    def deliveries(self, session_id):
        return self.requests.deliveries(session_id)

    def dismiss_delivery(self, session_id, request_id):
        return self.requests.dismiss_delivery(session_id, request_id)

    def dispatch(self, session_id, request_id, action, payload):
        if session_id != SESSION_ID:
            raise KeyError(session_id)
        if action in {"sendMessage", "followUp"}:
            fingerprint = hashlib.sha256(json.dumps([action, payload], sort_keys=True).encode()).hexdigest()
            existing = self.requests.reserve(
                SESSION_ID,
                request_id,
                fingerprint,
                recovery={
                    "kind": "message",
                    "sessionId": session_id,
                    "action": action,
                    "message": payload["message"],
                    "images": payload.get("images") or [],
                },
            )
            if existing is not None:
                return existing
            self.messages.append({"role": "user", "content": payload["message"]})
        elif action == "setThinking":
            self.thinking = payload["level"]
        else:
            raise ValueError("unsupported fixture operation")
        self.publish()
        outcome = {"requestId": request_id, "status": "accepted", "acceptance": "accepted"}
        if action in {"sendMessage", "followUp"}:
            self.requests.complete(SESSION_ID, request_id, fingerprint, outcome)
        return outcome


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("label")
    args = parser.parse_args()
    server.auth_manager()
    server.cursor_store()
    server._rpc_manager = BrowserSession(args.label)
    httpd = server.ThreadingHTTPServer((server.HOST, server.PORT), server.Handler)

    def terminate(_signum, _frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, terminate)
    print("Fleet browser fixture ready", flush=True)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
