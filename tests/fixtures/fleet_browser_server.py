"""Real Deck HTTP/SSE boundaries with deterministic in-memory OMP snapshots."""

import argparse
import base64
import hashlib
import signal

from omp_deck import attachments, server
from omp_deck.rpc_adapter import AdapterSnapshot

SESSION_ID = "same-local-session"


class BrowserSession:
    def __init__(self, label):
        self.label = label
        self.sequence = 0
        self.thinking = "medium"
        self.messages = [{"role": "assistant", "content": f"{label} reply"}]
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
        server._fleet_model.load_record(
            {
                "id": SESSION_ID,
                "title": "Shared project",
                "project": {"name": "demo", "path": str(server.SETTINGS.project_root / "demo")},
                "adapterKind": "rpc",
                "ompSessionId": "same-omp-session",
                "generation": 1,
                "updatedAt": "2026-09-01T00:00:00+00:00",
            }
        )
        self.publish()

    def publish(self):
        self.sequence += 1
        server._fleet_model.apply_rpc_snapshot(
            SESSION_ID,
            instance_id="browser-fixture",
            generation=1,
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

    def dispatch(self, session_id, request_id, action, payload):
        if session_id != SESSION_ID:
            raise KeyError(session_id)
        if action in {"sendMessage", "followUp"}:
            self.messages.append({"role": "user", "content": payload["message"]})
        elif action == "setThinking":
            self.thinking = payload["level"]
        else:
            raise ValueError("unsupported fixture operation")
        self.publish()
        return {"requestId": request_id, "status": "accepted"}


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
