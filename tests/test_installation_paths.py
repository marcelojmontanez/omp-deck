import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from omp_deck.installation_paths import mark_owned, remove_owned_root


class InstallationPathTests(unittest.TestCase):
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
