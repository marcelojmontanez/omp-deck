import base64
import json
import socket
import threading
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace

import attachments
import process_discovery
import server


class SessionParserTests(unittest.TestCase):
    def write_session(self, events):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "session.jsonl"
        path.write_text("".join(json.dumps(event) + "\n" for event in events))
        return path

    def header(self, path):
        return {
            "path": path,
            "title": "Test OMP",
            "sessionId": "session-1",
            "model": "provider/model",
            "thinking": "high",
        }

    def test_cross_site_browser_requests_are_rejected(self):
        self.assertFalse(server.trusted_browser_request({
            "Host": "desktop.example.ts.net",
            "Origin": "https://attacker.example",
            "Sec-Fetch-Site": "cross-site",
        }))
        self.assertTrue(server.trusted_browser_request({
            "Host": "desktop.example.ts.net",
            "Origin": "https://desktop.example.ts.net",
            "Sec-Fetch-Site": "same-origin",
        }))

    def test_registered_blank_session_is_visible_before_first_message(self):
        parsed = server.parse_session({
            "path": None,
            "sessionId": "blank-session",
            "model": "provider/model",
            "thinking": "low",
            "registeredAt": "2026-08-31T00:00:00+00:00",
        })

        self.assertEqual(parsed["sessionId"], "blank-session")
        self.assertEqual(parsed["status"], "idle")
        self.assertEqual(parsed["activity"], [])

    def test_session_changes_project_latest_model_and_thinking_level(self):
        path = self.write_session([
            {"type": "model_change", "model": "provider/new-model"},
            {"type": "thinking_level_change", "thinkingLevel": "minimal"},
        ])

        parsed = server.parse_session(self.header(path))

        self.assertEqual(parsed["model"], "provider/new-model")
        self.assertEqual(parsed["thinking"], "minimal")
        self.assertEqual(parsed["status"], "idle")

    def test_completed_turn_becomes_colored_structured_activity(self):
        path = self.write_session(
            [
                {
                    "type": "message",
                    "id": "user-1",
                    "timestamp": "2026-08-31T00:00:00Z",
                    "message": {"role": "user", "content": [{"type": "text", "text": "Fix mobile input"}]},
                },
                {
                    "type": "message",
                    "id": "assistant-1",
                    "message": {
                        "role": "assistant",
                        "stopReason": "toolUse",
                        "content": [{"type": "thinking", "thinking": "private reasoning"}],
                    },
                },
                {
                    "type": "custom",
                    "customType": "tool_execution_start",
                    "id": "tool-start",
                    "data": {
                        "toolCallId": "call-1",
                        "toolName": "read",
                        "intent": "Reading mobile styles",
                        "args": {"path": "static/index.html"},
                    },
                },
                {
                    "type": "message",
                    "id": "tool-result",
                    "message": {"role": "toolResult", "toolCallId": "call-1", "toolName": "read", "content": []},
                },
                {
                    "type": "message",
                    "id": "assistant-2",
                    "timestamp": "2026-08-31T00:01:00Z",
                    "message": {
                        "role": "assistant",
                        "stopReason": "stop",
                        "content": [{"type": "text", "text": "Input now persists."}],
                    },
                },
            ]
        )

        parsed = server.parse_session(self.header(path))

        self.assertEqual(parsed["status"], "idle")
        self.assertEqual([item["kind"] for item in parsed["activity"]], ["user", "assistant"])
        self.assertEqual(parsed["preview"], "Input now persists.")
        self.assertEqual(parsed["responseAt"], "2026-08-31T00:01:00Z")
        self.assertNotIn("private reasoning", json.dumps(parsed))

    def test_reset_boundary_hides_prior_conversation(self):
        path = self.write_session([
            {"type": "message", "timestamp": "2026-08-31T00:00:00Z",
             "message": {"role": "user", "content": [{"type": "text", "text": "Old request"}]}},
            {"type": "message", "timestamp": "2026-08-31T00:00:01Z",
             "message": {"role": "assistant", "stopReason": "stop",
                         "content": [{"type": "text", "text": "Old response"}]}},
            {"type": "reset_boundary", "timestamp": "2026-08-31T00:00:02Z"},
        ])

        parsed = server.parse_session(self.header(path))

        self.assertEqual(parsed["activity"], [])
        self.assertEqual(parsed["preview"], "")
        self.assertIsNone(parsed["responseAt"])


    def test_tool_result_waiting_for_next_turn_is_working(self):
        path = self.write_session(
            [
                {
                    "type": "message",
                    "message": {"role": "assistant", "stopReason": "toolUse", "content": []},
                },
                {
                    "type": "custom",
                    "customType": "tool_execution_start",
                    "data": {"toolCallId": "call-1", "toolName": "bash", "intent": "Running check", "args": {"command": "true"}},
                },
                {
                    "type": "message",
                    "message": {"role": "toolResult", "toolCallId": "call-1", "toolName": "bash", "content": []},
                },
            ]
        )

        self.assertEqual(server.parse_session(self.header(path))["status"], "working")
        self.assertEqual(server.parse_session(self.header(path))["workingOn"], "Running check")

    def test_yield_result_marks_subagent_idle(self):
        path = self.write_session([
            {
                "type": "message",
                "message": {"role": "assistant", "stopReason": "toolUse", "content": []},
            },
            {
                "type": "custom",
                "customType": "tool_execution_start",
                "data": {
                    "toolCallId": "yield-1",
                    "toolName": "yield",
                    "intent": "Submitting result",
                },
            },
            {
                "type": "message",
                "message": {
                    "role": "toolResult",
                    "toolCallId": "yield-1",
                    "toolName": "yield",
                    "content": [{"type": "text", "text": "Result submitted."}],
                },
            },
        ])

        parsed = server.parse_session(self.header(path))

        self.assertEqual(parsed["status"], "idle")
        self.assertIsNone(parsed["workingOn"])

    def test_new_user_message_does_not_reuse_previous_tool_intent(self):
        path = self.write_session([
            {"type": "custom", "customType": "tool_execution_start", "data": {
                "toolCallId": "old-call", "toolName": "bash", "intent": "Deploying old change",
            }},
            {"type": "message", "message": {
                "role": "toolResult", "toolCallId": "old-call", "content": [],
            }},
            {"type": "message", "message": {
                "role": "user", "content": [{"type": "text", "text": "New request"}],
            }},
        ])

        parsed = server.parse_session(self.header(path))

        self.assertEqual(parsed["workingOn"], "Preparing a response")

    def test_completed_assistant_clears_stale_working_state(self):
        path = self.write_session([
            {
                "type": "custom",
                "customType": "tool_execution_start",
                "data": {"toolCallId": "stale-call", "toolName": "read", "intent": "Reading image"},
            },
            {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "stopReason": "stop",
                    "content": [{"type": "text", "text": "Finished."}],
                },
            },
        ])

        self.assertEqual(server.parse_session(self.header(path))["status"], "idle")

    def test_unanswered_ask_requires_input(self):
        path = self.write_session(
            [
                {
                    "type": "custom",
                    "customType": "tool_execution_start",
                    "data": {"toolCallId": "ask-1", "toolName": "ask", "intent": "Choosing deployment mode", "args": {}},
                }
            ]
        )

        self.assertEqual(server.parse_session(self.header(path))["status"], "needs-input")

    def test_current_title_record_wins_over_original_session_title(self):
        path = self.write_session(
            [
                {"type": "title", "title": "Current title"},
                {"type": "session", "id": "session-1", "title": "Original title"},
                {"type": "title_change", "title": "Older generated title"},
            ]
        )

        self.assertEqual(server.read_header(path)["title"], "Current title")

    def test_tool_failure_is_distinct_from_assistant_error(self):
        path = self.write_session(
            [
                {
                    "type": "message",
                    "message": {
                        "role": "toolResult",
                        "toolCallId": "call-1",
                        "toolName": "bash",
                        "isError": True,
                        "content": [{"type": "text", "text": "command failed"}],
                    },
                }
            ]
        )

        parsed = server.parse_session(self.header(path))

        self.assertEqual(
            parsed["activity"],
            [{
                "id": None,
                "kind": "error",
                "title": "Tool failed",
                "detail": "command failed",
                "time": None,
            }],
        )
        self.assertEqual(parsed["status"], "attention")
        self.assertEqual(parsed["workingOn"], "Recovering from an error")

    def test_full_transcript_content_is_not_truncated(self):
        messages = [
            {
                "type": "message",
                "id": f"assistant-{index}",
                "message": {
                    "role": "assistant",
                    "stopReason": "stop",
                    "content": [{"type": "text", "text": f"Message {index}"}],
                },
            }
            for index in range(20)
        ]
        full_response = "# Heading\n\n" + ("Complete response content. " * 80)
        messages[-1]["message"]["content"][0]["text"] = full_response
        path = self.write_session(messages)

        parsed = server.parse_session(self.header(path))

        self.assertEqual(len(parsed["activity"]), 20)
        self.assertEqual(parsed["activity"][-1]["detail"], full_response.rstrip())
        self.assertFalse(parsed["activity"][-1]["detail"].endswith("…"))

    def test_conversation_history_is_bounded(self):
        path = self.write_session(
            [
                {
                    "type": "message",
                    "message": {
                        "role": "assistant",
                        "stopReason": "stop",
                        "content": [{"type": "text", "text": f"Message {index}"}],
                    },
                }
                for index in range(server.MAX_CHAT_MESSAGES + 10)
            ]
        )

        parsed = server.parse_session(self.header(path))

        self.assertEqual(len(parsed["activity"]), server.MAX_CHAT_MESSAGES)
        self.assertEqual(parsed["activity"][0]["detail"], "Message 10")

    def test_reset_preserves_archive_and_exposes_current_context(self):
        path = self.write_session([
            {"type": "message", "id": "old", "message": {
                "role": "assistant", "stopReason": "stop",
                "content": [{"type": "text", "text": "Earlier response"}],
            }},
            {"type": "reset_boundary", "timestamp": "2026-08-31T00:00:02Z"},
            {"type": "message", "id": "new", "message": {
                "role": "assistant", "stopReason": "stop",
                "content": [{"type": "text", "text": "Current response"}],
            }},
        ])

        parsed = server.parse_session(self.header(path))

        self.assertEqual([item["id"] for item in parsed["archiveActivity"]], ["old"])
        self.assertEqual([item["id"] for item in parsed["activity"]], ["new"])
        self.assertEqual(parsed["contextResetAt"], "2026-08-31T00:00:02Z")
        self.assertEqual(parsed["responseId"], "new")

    def test_persisted_cursor_clears_inbox(self):
        path = self.write_session([{
            "type": "message", "id": "answer-1", "message": {
                "role": "assistant", "stopReason": "stop",
                "content": [{"type": "text", "text": "Done"}],
            },
        }])
        with tempfile.TemporaryDirectory() as directory:
            store = server.ReadCursorStore(Path(directory) / "cursors.json")
            with patch.object(server, "READ_CURSORS", store):
                self.assertEqual(server.parse_session(self.header(path))["inboxState"], "unread")
                store.set("session-1", "answer-1")
                self.assertEqual(server.parse_session(self.header(path))["inboxState"], "read")

    def test_snapshot_orders_unread_agents_before_status_and_label_tiebreakers(self):
        cases = [
            ("Read Urgent", "needs-input", "read"),
            ("Unread Zulu", "working", "unread"),
            ("Unread Alpha", "working", "unread"),
            ("Unread Idle", "idle", "unread"),
            ("Read Working", "working", "read"),
        ]
        processes = []
        sessions = {}
        for label, status, inbox_state in cases:
            session_id = label.casefold().replace(" ", "-")
            processes.append({
                "id": session_id,
                "label": label,
                "project": "bridge",
                "parentId": None,
                "bridgeConnected": True,
                "sessionHeader": {"sessionId": session_id},
            })
            sessions[session_id] = {
                "status": status,
                "inboxState": inbox_state,
                "activitySummary": status,
            }

        with (
            patch.object(server, "omp_processes", return_value=processes),
            patch.object(
                server,
                "parse_session",
                side_effect=lambda header: sessions[header["sessionId"]],
            ),
        ):
            agents = server.build_snapshot()["agents"]

        self.assertEqual(
            [agent["label"] for agent in agents],
            [
                "Unread Alpha",
                "Unread Zulu",
                "Unread Idle",
                "Read Urgent",
                "Read Working",
            ],
        )

    def test_cursor_advance_never_regresses(self):
        with tempfile.TemporaryDirectory() as directory:
            store = server.ReadCursorStore(Path(directory) / "cursors.json")
            events = ["answer-1", "answer-2"]
            store.advance("session-1", "answer-2", events)

            self.assertEqual(store.advance("session-1", "answer-1", events), "answer-2")
            self.assertEqual(store.get("session-1"), "answer-2")

    def test_stale_cursor_snapshot_cannot_overwrite_newer_cursor(self):
        with tempfile.TemporaryDirectory() as directory:
            store = server.ReadCursorStore(Path(directory) / "cursors.json")
            store.set("session-1", "answer-2")

            self.assertEqual(store.advance("session-1", "answer-1", ["answer-1"]), "answer-2")
            self.assertEqual(store.get("session-1"), "answer-2")

    def test_arbitrary_file_attachment_is_validated_and_saved(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        with patch.object(attachments, "UPLOAD_ROOT", Path(directory.name)):
            paths = attachments.save_attachments(
                [
                    {
                        "name": "notes.pdf",
                        "type": "application/pdf",
                        "data": "data:application/pdf;base64,"
                        + base64.b64encode(b"pdf").decode(),
                    }
                ]
            )

        self.assertEqual(len(paths), 1)
        self.assertEqual(paths[0].suffix, ".pdf")
        self.assertEqual(paths[0].read_bytes(), b"pdf")

    def test_attachment_validation_failure_leaves_no_saved_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            items = [
                {"name": "valid.txt", "data": base64.b64encode(b"valid").decode()},
                {"name": "invalid.txt", "data": "not base64"},
            ]
            with patch.object(attachments, "UPLOAD_ROOT", root):
                with self.assertRaisesRegex(ValueError, "invalid file data"):
                    attachments.save_attachments(items)

            self.assertEqual(list(root.iterdir()), [])

    def test_attachment_write_failure_removes_files_already_written(self):
        original_write_bytes = Path.write_bytes
        writes = 0

        def fail_second_write(path, content):
            nonlocal writes
            writes += 1
            if writes == 2:
                raise OSError("disk full")
            return original_write_bytes(path, content)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            items = [
                {"name": "one.txt", "data": base64.b64encode(b"one").decode()},
                {"name": "two.txt", "data": base64.b64encode(b"two").decode()},
            ]
            with (
                patch.object(attachments, "UPLOAD_ROOT", root),
                patch.object(Path, "write_bytes", fail_second_write),
            ):
                with self.assertRaisesRegex(OSError, "disk full"):
                    attachments.save_attachments(items)

            self.assertEqual(list(root.iterdir()), [])

    def test_failed_delivery_removes_every_saved_attachment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            items = [
                {"name": "one.txt", "data": base64.b64encode(b"one").decode()},
                {"name": "two.txt", "data": base64.b64encode(b"two").decode()},
            ]
            with (
                patch.object(attachments, "UPLOAD_ROOT", root),
                patch.object(server, "save_attachments", attachments.save_attachments),
                patch.object(server, "send_to_agent", side_effect=ConnectionError("rejected")),
            ):
                with self.assertRaises(ConnectionError):
                    server.deliver_message({"id": "session"}, "hello", items, None)

            self.assertEqual(list(root.iterdir()), [])

    def test_failed_delivery_preserves_error_when_cleanup_unlink_fails(self):
        first = SimpleNamespace(unlink=lambda **_kwargs: (_ for _ in ()).throw(OSError("busy")))
        removed = []
        second = SimpleNamespace(unlink=lambda **_kwargs: removed.append(True))
        with (
            patch.object(server, "save_attachments", return_value=[first, second]),
            patch.object(server, "send_to_agent", side_effect=ConnectionError("rejected")),
        ):
            with self.assertRaisesRegex(ConnectionError, "rejected"):
                server.deliver_message({"id": "session"}, "hello", [], None)

        self.assertEqual(removed, [True])

    def test_successful_delivery_retains_files_for_delayed_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            items = [{"name": "one.txt", "data": base64.b64encode(b"one").decode()}]
            with (
                patch.object(attachments, "UPLOAD_ROOT", root),
                patch.object(server, "save_attachments", attachments.save_attachments),
                patch.object(server, "send_to_agent"),
                patch.object(server, "remove_later") as remove_later,
            ):
                server.deliver_message({"id": "session"}, "hello", items, "low")

            saved_paths = remove_later.call_args.args[0]
            self.assertEqual(len(saved_paths), 1)
            self.assertTrue(saved_paths[0].exists())

    def test_tool_image_is_attached_to_next_assistant_message(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        digest = "a" * 64
        Path(directory.name, f"{digest}.webp").write_bytes(b"image")
        path = self.write_session(
            [
                {
                    "type": "message",
                    "message": {
                        "role": "toolResult",
                        "content": [
                            {"type": "image", "data": f"blob:sha256:{digest}"}
                        ],
                    },
                },
                {
                    "type": "message",
                    "message": {
                        "role": "assistant",
                        "stopReason": "stop",
                        "content": [{"type": "text", "text": "Here is the image."}],
                    },
                },
            ]
        )

        with patch.object(attachments, "BLOB_ROOT", Path(directory.name)):
            parsed = server.parse_session(self.header(path))

        self.assertEqual(
            parsed["activity"][0]["images"],
            [{"url": f"/api/blob/{digest}"}],
        )

    @patch("server.subprocess.run")
    def test_usage_snapshot_is_parsed_and_cached(self, run):
        payload = {
            "generatedAt": 1,
            "reports": [{"provider": "openai-codex", "limits": [{"label": "7 days"}]}],
        }
        run.return_value = SimpleNamespace(
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
        )
        server._usage_cache.update(at=0.0, value=None)

        first = server.usage_snapshot()
        second = server.usage_snapshot()

        self.assertEqual(first, payload)
        self.assertIs(second, first)
        run.assert_called_once_with(
            [server.OMP_BIN, "usage", "--json"],
            text=True,
            capture_output=True,
            timeout=15,
        )

    def test_live_delivery_uses_the_selected_omp_socket(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent = {"id": "session-id", "pid": 123}
            socket_path = root / "session-id.123.instance.sock"
            received = {}
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(socket_path))
            listener.listen(1)

            def accept_request():
                connection, _ = listener.accept()
                with connection:
                    payload = b""
                    while b"\n" not in payload:
                        payload += connection.recv(4096)
                    received.update(json.loads(payload))
                    connection.sendall(b'{"ok":true}' + b"\n")
                listener.close()

            thread = threading.Thread(target=accept_request)
            thread.start()
            with patch.object(server, "SOCKET_ROOT", root):
                server.send_to_agent(
                    agent,
                    "Continue directly",
                    [Path("/tmp/screenshot.png")],
                    "high",
                )
            thread.join(timeout=2)

            self.assertEqual(
                received,
                {
                    "message": "Continue directly",
                    "attachments": ["/tmp/screenshot.png"],
                    "thinking": "high",
                },
            )

    def test_live_delivery_rejects_an_unconnected_omp(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(server, "SOCKET_ROOT", Path(directory)):
                with self.assertRaisesRegex(ConnectionError, "run /reload-plugins"):
                    server.send_to_agent({"id": "session-id", "pid": 123}, "Hello")


    def test_slash_command_is_typed_through_the_omp_tmux_pane(self):
        completed = SimpleNamespace(returncode=0, stderr="")
        with (
            patch.object(process_discovery, "process_tmux_pane", return_value="%3"),
            patch.object(process_discovery.subprocess, "run", return_value=completed) as run,
            patch.object(process_discovery.time, "sleep") as sleep,
        ):
            process_discovery.send_slash_command({"pid": 123}, "/clear")

        self.assertEqual(
            [invocation.args[0] for invocation in run.call_args_list],
            [
                ["tmux", "send-keys", "-t", "%3", "-l", "/"],
                ["tmux", "send-keys", "-t", "%3", "-l", "clear"],
                ["tmux", "send-keys", "-t", "%3", "Enter"],
            ],
        )
        self.assertEqual([invocation.args[0] for invocation in sleep.call_args_list], [0.3, 0.1, 0.35])


class LifecycleActionTests(unittest.TestCase):
    def test_interrupt_rejects_idle_session_before_signaling(self):
        handler = object.__new__(server.Handler)
        agent = {"pid": 123, "sessionHeader": {"path": None}}
        with (
            patch.object(handler, "session_agent", return_value=agent),
            patch.object(server, "parse_session", return_value={"executionState": "idle"}),
            patch.object(process_discovery, "process_tmux_pane") as pane,
        ):
            with self.assertRaisesRegex(ValueError, "not currently working"):
                handler.perform_action("session-1", "interrupt")

        pane.assert_not_called()

    def test_interrupt_types_control_c_only_for_working_session(self):
        handler = object.__new__(server.Handler)
        agent = {"pid": 123, "sessionHeader": {"path": None}}
        completed = SimpleNamespace(returncode=0, stderr="")
        with (
            patch.object(handler, "session_agent", return_value=agent),
            patch.object(server, "parse_session", return_value={"executionState": "working"}),
            patch.object(process_discovery, "process_tmux_pane", return_value="%7"),
            patch.object(server.subprocess, "run", return_value=completed) as run,
        ):
            result = handler.perform_action("session-1", "interrupt")

        self.assertEqual(run.call_args.args[0], ["tmux", "send-keys", "-t", "%7", "C-c"])
        self.assertEqual(result["action"], "interrupt")


class ProjectLauncherTests(unittest.TestCase):
    def test_projects_are_immediate_visible_dev_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Zulu").mkdir()
            (root / "alpha").mkdir()
            (root / ".hidden").mkdir()
            (root / "notes.txt").write_text("")
            with patch.object(server, "DEV_ROOT", root):
                self.assertEqual(server.launchable_projects(), ["alpha", "Zulu"])

    def test_launch_uses_validated_project_without_a_shell(self):
        completed = SimpleNamespace(returncode=0, stderr="")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "my-project"
            project.mkdir()
            with (
                patch.object(server, "DEV_ROOT", root),
                patch.object(server, "OMP_BIN", "/usr/bin/omp"),
                patch.object(server.subprocess, "run", return_value=completed) as run,
            ):
                result = server.launch_project("my-project")

        command = run.call_args.args[0]
        self.assertEqual(command[:3], ["tmux", "new-session", "-d"])
        self.assertEqual(command[5:], ["-c", str(project), "/usr/bin/omp", "--cwd", str(project)])
        self.assertEqual(result["project"], "my-project")

    def test_launch_can_create_a_new_project_directory(self):
        completed = SimpleNamespace(returncode=0, stderr="")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch.object(server, "DEV_ROOT", root),
                patch.object(server.subprocess, "run", return_value=completed),
            ):
                result = server.launch_project("new project", create=True)

            self.assertTrue((root / "new project").is_dir())
            self.assertTrue(result["created"])

    def test_failed_launch_removes_new_empty_directory(self):
        failed = SimpleNamespace(returncode=1, stderr="tmux failed")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch.object(server, "DEV_ROOT", root),
                patch.object(server.subprocess, "run", return_value=failed),
            ):
                with self.assertRaisesRegex(RuntimeError, "tmux failed"):
                    server.launch_project("new-project", create=True)

            self.assertFalse((root / "new-project").exists())

    def test_launch_rejects_paths_outside_dev_root(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(server, "DEV_ROOT", Path(directory)):
                with self.assertRaisesRegex(ValueError, "invalid project"):
                    server.launch_project("../tmp")


class ProcessDiscoveryMergeTests(unittest.TestCase):
    def test_socket_registered_process_wins_duplicate_session(self):
        stale = {"id": "same-session", "pid": 100}
        connected = {"id": "same-session", "pid": 200}
        socket_path = SimpleNamespace(is_socket=lambda: True)
        with (
            patch.object(process_discovery, "omp_processes", return_value=[stale]),
            patch.object(process_discovery, "socket_registered_processes", return_value=[connected]),
            patch.object(server, "agent_socket_path", return_value=socket_path),
        ):
            agents = server.omp_processes()

        self.assertEqual(agents, [{**connected, "bridgeConnected": True}])


class ProcessSessionAssociationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name) / "sessions"
        self.root.mkdir()
        self.process_dir = Path(directory.name) / "proc" / "123"
        self.process_dir.mkdir(parents=True)
        self.session_path = self.root / "project" / "session.jsonl"
        self.session_path.parent.mkdir()
        self.session_path.write_text("")

    def test_top_level_session_is_not_a_subagent(self):
        self.assertFalse(process_discovery.is_subagent_session(self.session_path, self.root))

    def test_nested_agent_session_is_a_subagent(self):
        nested = self.session_path.parent / "parent-session" / "Scout.jsonl"
        nested.parent.mkdir()
        nested.write_text("")

        self.assertTrue(process_discovery.is_subagent_session(nested, self.root))

    def test_process_session_path_accepts_split_resume_flag_without_open_descriptor(self):
        command = ["omp", "--resume", str(self.session_path)]

        self.assertEqual(
            process_discovery.process_session_path(self.process_dir, command, self.root),
            self.session_path.resolve(),
        )

    def test_process_session_path_accepts_equals_resume_flag_without_open_descriptor(self):
        command = ["omp", f"--resume={self.session_path}"]

        self.assertEqual(
            process_discovery.process_session_path(self.process_dir, command, self.root),
            self.session_path.resolve(),
        )

    def test_process_session_path_rejects_resume_path_outside_session_root(self):
        outside_path = self.root.parent / "outside.jsonl"
        outside_path.write_text("")

        self.assertIsNone(
            process_discovery.process_session_path(
                self.process_dir,
                ["omp", "--resume", str(outside_path)],
                self.root,
            )
        )


if __name__ == "__main__":
    unittest.main()
