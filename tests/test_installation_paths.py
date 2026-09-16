import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from omp_deck._platform_paths import config_home, data_home, state_home
from omp_deck.installation_paths import mark_owned, remove_owned_root


class InstallationPathTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "XDG defaults are POSIX-only")
    def test_empty_xdg_variables_use_home_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            with (
                patch.object(Path, "home", return_value=home),
                patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": "", "XDG_DATA_HOME": "", "XDG_STATE_HOME": ""},
                ),
            ):
                self.assertEqual(config_home(), home / ".config")
                self.assertEqual(data_home(), home / ".local/share")
                self.assertEqual(state_home(), home / ".local/state")

    def test_purge_requires_exact_approved_owned_root(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            owned = home / ".local/state/omp-deck"
            mark_owned(owned)
            (owned / "state.json").write_text("{}")

            remove_owned_root(owned, (owned,))

            self.assertFalse(owned.exists())

    def test_purge_rejects_unowned_and_symlink_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            unowned = home / ".local/state/omp-deck"
            unowned.mkdir(parents=True)
            with self.assertRaisesRegex(RuntimeError, "unowned"):
                remove_owned_root(unowned, (unowned,))

            target = home / ".local/state/real-deck"
            mark_owned(target)
            linked = home / ".local/state/omp-deck-link"
            linked.symlink_to(target, target_is_directory=True)
            with self.assertRaisesRegex(RuntimeError, "symlink"):
                remove_owned_root(linked, (linked,))

    @unittest.skipIf(os.name == "nt", "creating symlinks requires Windows developer mode")
    def test_purge_rejects_owned_root_beneath_symlinked_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            actual_parent = home / "actual"
            owned = actual_parent / "omp-deck"
            mark_owned(owned)
            sentinel = owned / "state.json"
            sentinel.write_text("{}")
            alias = home / "alias"
            alias.symlink_to(actual_parent, target_is_directory=True)
            aliased_root = alias / "omp-deck"

            with self.assertRaisesRegex(RuntimeError, "symlink"):
                remove_owned_root(aliased_root, (aliased_root,))

            self.assertEqual(sentinel.read_text(), "{}")

    @unittest.skipIf(os.name == "nt", "POSIX ownership mode semantics")
    def test_purge_rejects_owned_root_that_became_world_writable(self):
        with tempfile.TemporaryDirectory() as directory:
            owned = Path(directory) / "state/omp-deck"
            mark_owned(owned)
            sentinel = owned / "state.json"
            sentinel.write_text("{}")
            owned.chmod(0o777)

            with self.assertRaisesRegex(RuntimeError, "unsafe"):
                remove_owned_root(owned, (owned,))

            self.assertEqual(sentinel.read_text(), "{}")

    def test_purge_rejects_home_and_xdg_roots_even_if_marked(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            state_home = home / "state"
            mark_owned(state_home)
            sentinel = state_home / "unrelated"
            sentinel.write_text("preserved")
            with patch.dict("os.environ", {"XDG_STATE_HOME": str(state_home)}):
                with self.assertRaises(RuntimeError):
                    remove_owned_root(state_home, (state_home,))
            self.assertEqual(sentinel.read_text(), "preserved")


if __name__ == "__main__":
    unittest.main()
