from __future__ import annotations

import unittest
from types import SimpleNamespace

from omp_deck._terminal_commands import TerminalCommandCoordinator


class CollabCommandTests(unittest.TestCase):
    def test_checkbox_actions_remain_single_native_labels(self) -> None:
        active = SimpleNamespace(kind="collab")
        for value in ("A", "Next", "Other"):
            with self.subTest(value=value):
                self.assertEqual(
                    TerminalCommandCoordinator._prepare(
                        active,
                        "respondToUi",
                        {"requestId": "12", "value": value},
                    ),
                    (12, value),
                )

    def test_checkbox_selected_label_arrays_are_rejected(self) -> None:
        active = SimpleNamespace(kind="collab")
        for value in (["A"], {"selected": "A"}, 1, False):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "must be a string"):
                TerminalCommandCoordinator._prepare(
                    active,
                    "respondToUi",
                    {"requestId": "12", "value": value},
                )


if __name__ == "__main__":
    unittest.main()
