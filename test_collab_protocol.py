from __future__ import annotations

import dataclasses
import json
import unittest

from cryptography.exceptions import InvalidTag

from omp_deck.collab_protocol import (
    COLLAB_PROTO,
    GUEST_FRAME_TYPES,
    HOST_FRAME_TYPES,
    ParsedCollabLink,
    b64url_decode,
    b64url_encode,
    decode_frame,
    encode_frame,
    open_frame,
    pack_envelope,
    parse_collab_link,
    seal_frame,
    unpack_envelope,
    validate_frame,
    validate_guest_frame,
    validate_host_frame,
)


class LinkTests(unittest.TestCase):
    room = "abcdefghijklmnop"
    key = bytes(range(32))
    token = bytes(range(32, 48))
    view = b64url_encode(key)
    full = b64url_encode(key + token)

    def assert_link(self, text: str, ws_url: str, *, writable: bool) -> None:
        parsed = parse_collab_link(text)
        self.assertEqual(parsed.ws_url, ws_url)
        self.assertEqual(parsed.room_id, self.room)
        self.assertEqual(parsed.key, self.key)
        self.assertEqual(parsed.write_token, self.token if writable else None)
        self.assertEqual(parsed.read_only, not writable)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            parsed.room_id = "changed"  # type: ignore[misc]

    def test_default_compact_view_and_full(self) -> None:
        self.assert_link(f"{self.room}.{self.view}", f"wss://my.omp.sh/r/{self.room}", writable=False)
        self.assert_link(f"{self.room}#{self.full}", f"wss://my.omp.sh/r/{self.room}", writable=True)
        self.assert_link(f"{self.room}%23{self.view}", f"wss://my.omp.sh/r/{self.room}", writable=False)

    def test_full_custom_and_localhost_forms(self) -> None:
        self.assert_link(f"wss://relay.example/r/{self.room}.{self.full}", f"wss://relay.example/r/{self.room}", writable=True)
        self.assert_link(f"relay.example:7443/r/{self.room}.{self.view}", f"wss://relay.example:7443/r/{self.room}", writable=False)
        self.assert_link(f"ws://localhost:8080/r/{self.room}.{self.view}", f"ws://localhost:8080/r/{self.room}", writable=False)
        self.assert_link(f"http://127.0.0.1/r/{self.room}#{self.view}", f"ws://127.0.0.1/r/{self.room}", writable=False)
        self.assert_link(f"ws://[::1]:9000/r/{self.room}.{self.view}", f"ws://[::1]:9000/r/{self.room}", writable=False)

    def test_browser_deep_link_under_safe_arbitrary_path(self) -> None:
        inner = f"relay.example/r/{self.room}.{self.full}"
        for path in ("/", "/collab", "/apps/view/session-1"):
            self.assert_link(f"https://viewer.example{path}#{inner}", f"wss://relay.example/r/{self.room}", writable=True)

    def test_rejections(self) -> None:
        invalid = (
            f"ws://relay.example/r/{self.room}.{self.view}",
            f"http://relay.example/r/{self.room}.{self.view}",
            f"wss://relay.example/r/short.{self.view}",
            f"wss://relay.example/r/{self.room}.AAAA",
            f"wss://relay.example/r/{self.room}.{self.view}?x=1",
            f"wss://relay.example/r/{self.room}.{self.view}#other",
            f"wss://user@relay.example/r/{self.room}.{self.view}",
            f"wss://relay.example/extra/r/{self.room}.{self.view}",
            f"https://viewer.example/path?theme=dark#{self.room}.{self.view}",
            f"{self.room}.{self.view}=",
            "",
        )
        for text in invalid:
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_collab_link(text)

    def test_repr_redacts_secrets(self) -> None:
        parsed = parse_collab_link(f"{self.room}.{self.full}")
        rendered = repr(parsed)
        self.assertNotIn(self.full, rendered)
        self.assertNotIn(repr(self.key), rendered)
        self.assertNotIn(repr(self.token), rendered)
        self.assertIn("<redacted>", rendered)


class EncodingTests(unittest.TestCase):
    def test_base64url_is_strict_and_unpadded(self) -> None:
        for raw in (b"\x00", b"hello", bytes(range(48))):
            self.assertEqual(b64url_decode(b64url_encode(raw)), raw)
            self.assertNotIn("=", b64url_encode(raw))
        for bad in ("", "YQ==", "a+", "a/", "A"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                b64url_decode(bad)

    def test_envelope_roundtrip_and_bounds(self) -> None:
        packed = pack_envelope(0xF1020304, b"sealed")
        self.assertEqual(unpack_envelope(packed), (0xF1020304, b"sealed"))
        for peer in (-1, 2**32, True):
            with self.assertRaises(ValueError):
                pack_envelope(peer, b"")
        with self.assertRaises(ValueError):
            unpack_envelope(b"abc")
        with self.assertRaises(ValueError):
            unpack_envelope(packed, max_frame_bytes=len(packed) - 1)

    def test_seal_roundtrip_tamper_and_wrong_key(self) -> None:
        key = bytes(range(32))
        frame = {"t": "hello", "proto": COLLAB_PROTO, "name": "Ada"}
        sealed = seal_frame(key, frame)
        self.assertEqual(len(sealed[:12]), 12)
        self.assertEqual(open_frame(key, sealed, direction="guest"), frame)
        tampered = sealed[:-1] + bytes((sealed[-1] ^ 1,))
        with self.assertRaises(InvalidTag):
            open_frame(key, tampered)
        with self.assertRaises(InvalidTag):
            open_frame(bytes(reversed(key)), sealed)

    def test_json_object_and_frame_bounds(self) -> None:
        with self.assertRaises(ValueError):
            decode_frame(b"[]")
        with self.assertRaises(ValueError):
            decode_frame(b"not json")
        with self.assertRaises(ValueError):
            decode_frame(b'{"t":"hello"}', max_frame_bytes=5)
        with self.assertRaises(ValueError):
            encode_frame({"t": "prompt", "text": "large"}, max_frame_bytes=4)
        with self.assertRaises(ValueError):
            open_frame(bytes(32), b"too short")
        with self.assertRaises(ValueError):
            encode_frame({"t": "prompt", "number": float("nan")})

    def test_exact_discriminated_frames_and_nested_validation(self) -> None:
        guest = (
            {"t": "hello", "proto": COLLAB_PROTO, "name": "Ada"},
            {"t": "prompt", "text": "hi", "images": [{"type": "image", "data": "AA", "mimeType": "image/png"}]},
            {"t": "ui-response", "reqId": 1, "value": "yes"},
            {"t": "abort"},
            {"t": "agent-cmd", "cmd": "chat", "agentId": "a", "text": "hi"},
            {"t": "fetch-transcript", "reqId": 2, "agentId": "a", "fromByte": 0},
        )
        state = {"isStreaming": False, "queuedMessageCount": 0, "cwd": "/tmp", "participants": []}
        agent = {"id": "a", "displayName": "Main", "kind": "main", "status": "idle",
                 "hasSessionFile": True, "createdAt": 1, "lastActivity": 2}
        host = (
            {"t": "welcome", "proto": COLLAB_PROTO,
             "header": {"type": "session", "id": "s", "timestamp": "now", "cwd": "/tmp"},
             "state": state, "agents": [agent], "entryCount": 0},
            {"t": "snapshot-chunk", "entries": [{"type": "future", "payload": {"x": 1}}], "final": True},
            {"t": "entry", "entry": {"type": "future", "payload": []}},
            {"t": "event", "event": {"type": "future", "payload": None}},
            {"t": "state", "state": state},
            {"t": "bus", "channel": "task:subagent:lifecycle", "data": {"id": "a"}},
            {"t": "agents", "agents": [agent]},
            {"t": "ui-request", "request": {"kind": "select", "title": "Choose", "options": ["A", {"label": "B"}], "reqId": 3}},
            {"t": "ui-request-end", "reqId": 3},
            {"t": "transcript", "reqId": 2, "text": "", "newSize": 0},
            {"t": "bye", "reason": "done"},
            {"t": "error", "message": "no"},
        )
        for frame in guest:
            self.assertIs(validate_guest_frame(frame), frame)
            with self.assertRaises(ValueError): validate_host_frame(frame)
        for frame in host:
            self.assertIs(validate_host_frame(frame), frame)
            with self.assertRaises(ValueError): validate_guest_frame(frame)

        malformed = (
            {}, {"t": "abort", "extra": True}, {"t": "hello", "proto": True, "name": "x"},
            {"t": "prompt", "text": "x", "images": [{"type": "image", "data": "x"}]},
            {"t": "state", "state": {**state, "queuedMessageCount": -1}},
            {"t": "agents", "agents": [{**agent, "createdAt": True}]},
            {"t": "ui-request", "request": {"kind": "select", "title": "x", "options": [{"label": 1}], "reqId": 1}},
            {"t": "event", "event": {"type": "notice", "level": "debug", "message": "x"}},
        )
        for frame in malformed:
            with self.assertRaises(ValueError): validate_frame(frame)

    def test_decode_rejects_duplicate_keys(self) -> None:
        payload = b'{"t":"hello","t":"abort"}'
        with self.assertRaises(ValueError):
            decode_frame(payload)


if __name__ == "__main__":
    unittest.main()
