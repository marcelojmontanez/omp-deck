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
from types import SimpleNamespace
from unittest.mock import patch

from tools.release_lib import node, node_plugin, node_rollback
from tools.release_lib.node_common import ReleaseError, profile, read_file, restart_token, tree_digest

from omp_deck.installation_paths import mark_owned

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

    def test_changed_plugin_install_and_rollback_track_exact_owned_sources(self) -> None:
        store = self.root / "plugin-store"
        mark_owned(store, mode=0o700)
        original = store / "plugin-original"
        candidate = store / "plugin-candidate"
        original.mkdir()
        candidate.mkdir()
        (original / "package.json").write_text('{"name":"omp-deck","version":"1"}')
        (original / "index.js").write_text("old")
        (candidate / "package.json").write_text('{"name":"omp-deck","version":"2"}')
        (candidate / "index.js").write_text("new")
        active = {"source": original}

        def release_run(_profile, arguments, **_kwargs):
            arguments = [str(value) for value in arguments]
            if arguments[1:4] == ["plugin", "list", "--json"]:
                payload = {"npm": [{"name": "omp-deck", "path": str(active["source"])}]}
                return SimpleNamespace(returncode=0, stdout=json.dumps(payload).encode())
            if "--json" in arguments:
                active["source"] = candidate
                payload = {"version": 1, "pluginSource": str(candidate)}
                return SimpleNamespace(returncode=0, stdout=json.dumps(payload).encode())
            if arguments[1:3] == ["plugin", "install"]:
                active["source"] = Path(arguments[3])
                return SimpleNamespace(returncode=0, stdout=b"")
            raise AssertionError(arguments)

        release_profile = SimpleNamespace(
            omp=self.root / "omp",
            tool_python=self.root / "tool/bin/python",
            plugin_expected=self.root / "obsolete-environment/plugin",
        )
        with (
            patch("omp_deck._plugin_lifecycle.plugin_storage_root", return_value=store),
            patch.object(node_plugin, "run", side_effect=release_run),
        ):
            installed = node_plugin.install_plugin(
                release_profile,
                tree_digest(candidate),
                {},
                original,
            )
            self.assertEqual(installed, candidate.resolve())
            self.assertEqual(active["source"], candidate)
            self.assertTrue(original.is_dir())
            restored = node_plugin.restore_plugin(
                release_profile,
                tree_digest(original),
                {},
                original,
                self.root / "guard/snapshots/plugin",
            )
        self.assertEqual(restored, original.resolve())
        self.assertEqual(active["source"].resolve(), original.resolve())
        self.assertFalse(candidate.exists())

    def test_legacy_plugin_backup_is_republished_to_owned_storage_on_rollback(self) -> None:
        store = self.root / "plugin-store"
        mark_owned(store, mode=0o700)
        candidate = store / "plugin-candidate"
        candidate.mkdir()
        (candidate / "package.json").write_text('{"name":"omp-deck","version":"2"}')
        (candidate / "index.js").write_text("new")
        backup = self.root / "guard/snapshots/plugin"
        backup.mkdir(parents=True)
        (backup / "package.json").write_text('{"name":"omp-deck","version":"1"}')
        (backup / "index.js").write_text("old")
        active = {"source": candidate, "version": "2"}

        def release_run(_profile, arguments, **_kwargs):
            arguments = [str(value) for value in arguments]
            if arguments[1:4] == ["plugin", "list", "--json"]:
                record = {
                    "name": "omp-deck",
                    "path": str(active["source"]),
                    "version": active["version"],
                    "manifest": {"version": active["version"]},
                }
                return SimpleNamespace(returncode=0, stdout=json.dumps({"npm": [record]}).encode())
            if arguments[1:4] == ["plugin", "doctor", "--json"]:
                checks = [{"name": "plugin:omp-deck", "status": "ok"}]
                return SimpleNamespace(returncode=0, stdout=json.dumps(checks).encode())
            if arguments[1:3] == ["plugin", "install"]:
                active.update(source=Path(arguments[3]), version="1")
                return SimpleNamespace(returncode=0, stdout=b"")
            raise AssertionError(arguments)

        release_profile = SimpleNamespace(
            omp=self.root / "omp",
            tool_python=self.root / "tool/bin/python",
            plugin_expected=self.root / "tool/lib/omp_deck/plugin",
        )
        with (
            patch("omp_deck._plugin_lifecycle.plugin_storage_root", return_value=store),
            patch.object(node_plugin, "run", side_effect=release_run),
        ):
            restored = node_plugin.restore_plugin(
                release_profile,
                tree_digest(backup),
                {},
                release_profile.plugin_expected,
                backup,
            )
        self.assertEqual(active["source"].resolve(), restored)
        self.assertEqual(restored.parent, store.resolve())
        self.assertEqual(tree_digest(restored), tree_digest(backup))
        self.assertFalse(candidate.exists())

    def test_rollback_restores_when_failed_install_removed_tool_root(self) -> None:
        tool_root = self.root / "tool"
        backup = self.root / "guard/snapshots/uv-tool-environment"
        backup.mkdir(parents=True)
        (backup / "identity").write_text("retained")
        plugin_backup = self.root / "guard/snapshots/plugin"
        plugin_backup.mkdir()
        (plugin_backup / "package.json").write_text('{"name":"omp-deck"}')
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
                "pluginBackup": str(plugin_backup),
                "serviceEnvironment": {},
                "serviceEnabled": True,
            },
            "cutover": {
                "registryStable": [],
                "authSha256": hashlib.sha256(b"auth").hexdigest(),
            },
            "loopbackHTTP": None,
        }
        fake_profile = type(
            "Profile",
            (),
            {"tool_root": tool_root, "auth": self.root / "auth", "plugin_expected": self.root / "plugin"},
        )()
        (self.root / "auth").write_text("auth")
        with (
            patch.object(node, "_guard_files"),
            patch.object(node, "_verify_staged"),
            patch.object(node, "running", return_value=False),
            patch.object(node_rollback, "quiescent", return_value=True),
            patch.object(node, "_environment_identity", return_value={"version": "1", "revision": "a" * 40}),
            patch.object(node, "_installed", return_value={"version": "1", "revision": "a" * 40}),
            patch.object(node_plugin, "restore_plugin", return_value=self.root / "restored-plugin"),
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
