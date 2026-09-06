import tempfile
import unittest
from pathlib import Path

from omp_deck.projects import (
    create_project_directory,
    list_launchable_projects,
    resolve_project_path,
)


class ProjectPathTests(unittest.TestCase):
    def test_rejects_symlinked_project_and_root(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "projects"
            outside = base / "outside"
            root.mkdir()
            outside.mkdir()
            (root / "escape").symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "invalid project"):
                resolve_project_path(root, "escape")
            self.assertNotIn("escape", list_launchable_projects(root))

            linked_root = base / "linked-projects"
            linked_root.symlink_to(root, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "invalid project root"):
                resolve_project_path(linked_root, "new", must_exist=False)

    def test_rejects_broken_links_and_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "broken").symlink_to(root / "missing", target_is_directory=True)
            (root / "file").write_text("not a directory")

            for project in ("broken", "file"):
                with self.subTest(project=project):
                    with self.assertRaisesRegex(ValueError, "invalid project"):
                        resolve_project_path(root, project)

    def test_new_project_requires_existing_safe_parent_and_revalidates_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = resolve_project_path(root, "new", must_exist=False)
            candidate.mkdir()
            self.assertEqual(resolve_project_path(root, "new"), candidate)

            with self.assertRaisesRegex(ValueError, "invalid project"):
                resolve_project_path(root, "missing/nested", must_exist=False)

    def test_rejects_noncanonical_and_control_character_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "parent").mkdir()
            invalid = (
                " parent/new",
                "parent/new ",
                "parent//new",
                "parent/./new",
                "parent/new/",
                "parent\\new",
                "parent/\nnew",
                "parent/\x7fnew",
            )

            for project in invalid:
                with self.subTest(project=project):
                    with self.assertRaisesRegex(ValueError, "invalid project"):
                        resolve_project_path(root, project, must_exist=False)

    def test_creation_uses_directory_descriptors_and_rejects_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = root / "parent"
            parent.mkdir()

            created = create_project_directory(root, "parent/new")
            self.assertEqual(created, parent / "new")

            created.rmdir()
            created.symlink_to(root, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "invalid project|already exists"):
                create_project_directory(root, "parent/new")

    def test_discovery_depth_bounds_nested_project_search(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nested = root / "group" / "project" / "nested-repository"
            nested.mkdir(parents=True)
            (nested / "pyproject.toml").write_text("[project]\nname='nested'\n")

            self.assertEqual(
                list_launchable_projects(root, max_depth=2),
                ["group", "group/project"],
            )
            self.assertEqual(
                list_launchable_projects(root, max_depth=3),
                ["group", "group/project", "group/project/nested-repository"],
            )


if __name__ == "__main__":
    unittest.main()
