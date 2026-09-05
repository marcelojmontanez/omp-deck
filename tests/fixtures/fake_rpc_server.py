#!/usr/bin/env python3
"""Deterministic JSONL subprocess used only by RpcClient compatibility tests."""

import base64
import json
import signal
import sys
import time

SCENARIO = sys.argv[1]
TRANSCRIPT = sys.argv[2] if len(sys.argv) > 2 else None
MAX_FRAME = 1024 * 1024
MAX_REASSEMBLED = 64 * 1024 * 1024


def emit(payload):
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def record(payload):
    if TRANSCRIPT:
        with open(TRANSCRIPT, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, separators=(",", ":")) + "\n")


def response(request, data=None):
    emit({"type": "response", "id": request["id"], "command": request["type"], "success": True, "data": data or {}})


def ready(v2=True):
    payload = {"type": "ready", "protocolVersion": 1}
    if v2:
        payload.update(
            {
                "supportedProtocolVersions": [1, 2],
                "maxFrameBytes": MAX_FRAME,
                "maxReassembledFrameBytes": MAX_REASSEMBLED,
            }
        )
    emit(payload)


def read_request():
    line = sys.stdin.readline()
    if not line:
        raise EOFError
    payload = json.loads(line)
    record(payload)
    return payload


def negotiate():
    request = read_request()
    if request["type"] != "negotiate_protocol" or request.get("protocolVersion") != 2:
        raise RuntimeError(f"unexpected negotiation: {request!r}")
    response(request, {"protocolVersion": 2})


def chunk(payload, *, corrupt=None):
    encoded = json.dumps(payload, separators=(",", ":")).encode()
    parts = [encoded[index : index + 256 * 1024] for index in range(0, len(encoded), 256 * 1024)]
    for index, part in enumerate(parts):
        frame = {
            "type": "rpc_chunk",
            "chunkId": "fixture-chunk",
            "index": index,
            "count": len(parts),
            "byteLength": len(encoded),
            "data": base64.b64encode(part).decode(),
        }
        if corrupt == "base64" and index == 0:
            frame["data"] = "not canonical base64!"
        emit(frame)


if SCENARIO == "startup_timeout":
    time.sleep(30)
    raise SystemExit

ready(SCENARIO in {"contract", "ask_flow", "valid_chunk", "malformed_chunk", "oversized_chunk"})

if SCENARIO not in {"v1", "eof", "timeout", "out_of_order", "cleanup"}:
    negotiate()

if SCENARIO == "v1":
    request = read_request()
    response(request, {"sessionId": "v1-session"})
elif SCENARIO == "contract":
    emit(
        {
            "type": "extension_ui_request",
            "id": "ui-input",
            "method": "input",
            "title": "Question",
            "placeholder": "Answer",
        }
    )
    emit({"type": "extension_ui_request", "id": "ui-confirm", "method": "confirm", "message": "Proceed?"})
    while True:
        try:
            request = read_request()
        except EOFError:
            break
        kind = request["type"]
        if kind == "get_state":
            response(
                request,
                {
                    "sessionId": "fixture-session",
                    "sessionName": "Fixture",
                    "isStreaming": False,
                    "messageCount": 2,
                    "queuedMessageCount": 0,
                },
            )
        elif kind == "get_messages_page":
            response(
                request, {"messages": [{"role": "user", "content": "hello"}], "totalMessages": 1, "nextCursor": None}
            )
        else:
            response(request)
elif SCENARIO == "ask_flow":
    emit(
        {
            "type": "extension_ui_request",
            "id": "ask-input",
            "method": "input",
            "title": "Deployment",
            "message": "Name the target",
            "placeholder": "staging",
            "prefill": "stage-",
        }
    )
    emit(
        {
            "type": "extension_ui_request",
            "id": "ask-select",
            "method": "select",
            "title": "Region",
            "options": ["iad", "fra"],
            "optionDetails": [
                {"label": "Virginia", "description": "US East"},
                {"label": "Frankfurt", "description": "EU Central"},
            ],
        }
    )
    emit(
        {
            "type": "extension_ui_request",
            "id": "ask-confirm",
            "method": "confirm",
            "message": "Proceed?",
            "timeout": 60000,
        }
    )
    while True:
        try:
            request = read_request()
        except EOFError:
            break
        response(request)
elif SCENARIO == "out_of_order":
    first = read_request()
    second = read_request()
    for request in (second, first):
        if request["type"] == "get_state":
            response(request, {"sessionId": "correlated", "messageCount": 0})
        else:
            response(request, {"messages": [{"role": "user", "content": "correlated"}]})
elif SCENARIO == "valid_chunk":
    request = read_request()
    response_payload = {
        "type": "response",
        "id": request["id"],
        "command": request["type"],
        "success": True,
        "data": {"sessionId": "chunked", "sessionName": "x" * MAX_FRAME, "messageCount": 0},
    }
    chunk(response_payload)
elif SCENARIO == "malformed_chunk":
    request = read_request()
    payload = {"type": "response", "id": request["id"], "success": True, "data": {"padding": "x" * MAX_FRAME}}
    chunk(payload, corrupt="base64")
elif SCENARIO == "oversized_chunk":
    read_request()
    emit(
        {
            "type": "rpc_chunk",
            "chunkId": "too-large",
            "index": 0,
            "count": 1,
            "byteLength": MAX_REASSEMBLED + 1,
            "data": "eA==",
        }
    )
elif SCENARIO == "eof":
    read_request()
    raise SystemExit(7)
elif SCENARIO == "timeout":
    read_request()
    time.sleep(30)
elif SCENARIO == "cleanup":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    while sys.stdin.readline():
        pass
