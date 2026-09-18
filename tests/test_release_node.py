from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from tools.release_lib import node
from tools.release_lib.node_common import ReleaseError, profile, read_file, restart_token, tree_digest

if os.name != "nt":
    import pwd


def _profile(root: Path) -> dict[str, object]:
    value: dict[str, object] = {
        "platform": sys.platform,
        "user": pwd.getpwuid(os.geteuid()).pw_name,
        "home": str(Path.home()),
        "uv": str(root / "uv"),
        "base_python": str(root / "python"),
        "omp": str(root / "omp"),
        "tool_root": str(root / "tool"),
        "entrypoint": str(root / "entry"),
        "config": str(root / "config.json"),
        "unit": str(root / "unit"),
        "state_root": str(root / "state"),
        "plugin_expected": str(root / "plugin"),
        "service_name": "deck.service",
        "rollback_root": str(root / "rollbacks"),
        "port": 8787,
    }
    if sys.platform == "darwin":
        value.update(launch_domain="gui/1", launch_label="deck")
    return value


@unittest.skipIf(os.name == "nt", "release guards target macOS and Linux services")
class ReleaseNodeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_profile_rejects_relative_private_paths(self) -> None:
        value = _profile(self.root)
        value["state_root"] = "relative"
        with self.assertRaisesRegex(ReleaseError, "absolute"):
            profile(value)

    def test_owned_reader_rejects_symlink(self) -> None:
        target = self.root / "target"
        target.write_text("secret")
        link = self.root / "link"
        link.symlink_to(target)
        with self.assertRaisesRegex(ReleaseError, "unsafe"):
            read_file(link)

    def test_tree_digest_rejects_plugin_symlink(self) -> None:
        plugin = self.root / "plugin"
        plugin.mkdir()
        (plugin / "real").write_text("x")
        (plugin / "alias").symlink_to(plugin / "real")
        with self.assertRaisesRegex(ReleaseError, "symlink"):
            tree_digest(plugin)

    def test_artifact_requires_exact_wheel_and_plugin_identity(self) -> None:
        path = self.root / "omp_deck-1.2.3-py3-none-any.whl"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("omp_deck-1.2.3.dist-info/METADATA", "Name: omp-deck\nVersion: 1.2.3\n")
            archive.writestr("omp_deck/plugin/index.js", "plugin")
        value = {
            "wheelPath": str(path),
            "wheelSha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "wheelVersion": "1.2.3",
            "revision": "a" * 40,
            "pluginSha256": hashlib.sha256(b"index.js\0plugin\0").hexdigest(),
        }
        self.assertEqual(node._artifact(value, "candidate")["wheelVersion"], "1.2.3")
        value["pluginSha256"] = "0" * 64
        with self.assertRaisesRegex(ReleaseError, "identity mismatch"):
            node._artifact(value, "candidate")

    def test_watchdog_marker_must_be_fresh_and_live(self) -> None:
        marker = {
            "version": 1,
            "transaction": "release-1",
            "pid": os.getpid(),
            "nonce": "a" * 64,
            "readyAt": int(time.time()) - 121,
        }
        path = self.root / "watchdog-ready.json"
        path.write_text(json.dumps(marker))
        path.chmod(0o600)
        with self.assertRaisesRegex(ReleaseError, "stale"):
            node._ready(self.root, "release-1")

    def test_rollback_before_mutation_is_idempotent(self) -> None:
        guard = self.root / "guard"
        guard.mkdir()
        self.root.chmod(0o700)
        guard.chmod(0o700)
        manifest = {"version": 1, "transaction": "release-1", "state": "prepared", "deadline": None, "touched": False}
        node._save(self.root, manifest)
        node._rollback_locked(object(), self.root, manifest, None, "cancelled")
        node._rollback_locked(object(), self.root, manifest, None, "cancelled")
        self.assertEqual(node._manifest(self.root)["state"], "rolledback")

    def test_unregistered_live_rpc_prevents_restart(self):
        live = {"sessions": [{"id": "unknown", "transport": {"kind": "rpc"}}]}
        with self.assertRaisesRegex(ReleaseError, "ownership"):
            restart_token(live, {"sessions": []})

    def test_working_rpc_prevents_restart(self):
        live = {
            "sessions": [
                {
                    "id": "managed",
                    "state": "working",
                    "lifecycle": "ongoing",
                    "transport": {"kind": "rpc", "status": "connected", "instanceId": "native"},
                }
            ]
        }
        registry = {"sessions": [{"id": "managed", "adapterKind": "rpc", "desiredRpcRunning": True}]}
        with self.assertRaisesRegex(ReleaseError, "restart-safe"):
            restart_token(live, registry)

    def test_rollback_restores_when_failed_install_removed_tool_root(self) -> None:
        tool_root = self.root / "tool"
        backup = self.root / "backup"
        backup.mkdir()
        (backup / "identity").write_text("retained")
        manifest = {
            "version": 1,
            "transaction": "release-1",
            "state": "pending",
            "touched": True,
            "rollback": {"wheelVersion": "1", "revision": "a" * 40, "pluginSha256": "b" * 64},
            "candidate": {
                "wheelPath": str(self.root / "candidate"),
                "wheelSha256": "c" * 64,
                "wheelVersion": "2",
                "pluginSha256": "d" * 64,
            },
            "constraints": str(self.root / "constraints"),
            "constraintsSha256": "e" * 64,
            "originals": {
                "environmentBackup": str(backup),
                "pluginBackup": str(self.root / "plugin"),
                "serviceEnvironment": {},
                "serviceEnabled": True,
            },
            "cutover": {"registryStable": [], "authSha256": "unused"},
            "loopbackHTTP": None,
        }
        fake_profile = type("Profile", (), {"tool_root": tool_root, "auth": self.root / "auth"})()
        (self.root / "auth").write_text("auth")
        with (
            patch.object(node, "_guard_files"),
            patch.object(node, "_verify_staged"),
            patch.object(node, "running", return_value=False),
            patch.object(node, "quiescent", return_value=True),
            patch.object(node, "_installed", return_value={"version": "1", "revision": "a" * 40}),
            patch.object(node, "_install_plugin"),
            patch.object(node, "start"),
            patch.object(node, "health"),
            patch.object(node, "service_enabled", return_value=True),
            patch.object(node, "registry", return_value=({}, [])),
            patch.object(node, "_save"),
        ):
            node._rollback_locked(fake_profile, self.root, manifest, None, "automatic")
        self.assertEqual((tool_root / "identity").read_text(), "retained")
        self.assertEqual(manifest["state"], "rolledback")


if __name__ == "__main__":
    unittest.main()
