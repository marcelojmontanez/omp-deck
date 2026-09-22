import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]
RUN_PACKAGING_TESTS = os.environ.get("OMP_DECK_PACKAGING_TESTS") == "1"
PRODUCTION_VERSION_BASELINE = "fe1754b15098937b6bb82610a782782dcf58bfa6"


@unittest.skipUnless(RUN_PACKAGING_TESTS, "set OMP_DECK_PACKAGING_TESTS=1 to exercise isolated artifact builds")
class PackagingIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        temporary = Path(self.temporary.name)
        self.repository = temporary / "repository"
        self.outputs = temporary / "outputs"
        self.repository.mkdir()
        self.outputs.mkdir()
        self._run("git", "init", "--initial-branch=main")
        self._run("git", "config", "user.name", "Packaging Test")
        self._run("git", "config", "user.email", "packaging@example.invalid")
        self._run("git", "commit", "--allow-empty", "-m", "version baseline")
        self.version_baseline_revision = self._run("git", "rev-parse", "HEAD").stdout.strip()
        shutil.copytree(
            ROOT,
            self.repository,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns(
                ".git",
                ".ruff_cache",
                ".venv",
                "_build_identity.py",
                "_source_manifest.json",
                "build",
                "dist",
                "node_modules",
                "*.egg-info",
                "__pycache__",
            ),
        )
        backend = self.repository / "_build_backend.py"
        configured = backend.read_text(encoding="utf-8").replace(
            PRODUCTION_VERSION_BASELINE,
            self.version_baseline_revision,
        )
        self.assertNotIn(PRODUCTION_VERSION_BASELINE, configured)
        backend.write_text(configured, encoding="utf-8")
        self._run("git", "add", ".")
        self._run("git", "commit", "-m", "initial source revision")
        self.baseline_revision = self._run("git", "rev-parse", "HEAD").stdout.strip()

    def _run(self, *command, cwd=None, check=True, env=None):
        return subprocess.run(
            command,
            cwd=cwd or self.repository,
            check=check,
            capture_output=True,
            text=True,
            env={**os.environ, **(env or {}), "PYTHONDONTWRITEBYTECODE": "1"},
        )

    def _build(self, *kinds, cwd=None, output=None, check=True, env=None):
        destination = output or self.outputs
        return self._run(
            sys.executable,
            "-m",
            "build",
            "--no-isolation",
            "--outdir",
            str(destination),
            *kinds,
            cwd=cwd,
            check=check,
            env=env,
        )

    def _wheel_identity(self, wheel):
        script = (
            "import json, sys; "
            "sys.path.insert(0, sys.argv[1]); "
            "import omp_deck; from importlib.metadata import version; "
            "print(json.dumps({'version': omp_deck.__version__, 'identity': omp_deck.build_identity(), "
            "'metadataVersion': version('omp-deck')}))"
        )
        completed = self._run(sys.executable, "-I", "-c", script, str(wheel), cwd=self.outputs)
        return json.loads(completed.stdout)

    def test_successive_commits_and_repeated_builds_have_stable_distinct_identities(self):
        first_output = self.outputs / "first"
        repeated_output = self.outputs / "repeated"
        second_output = self.outputs / "second"
        first_output.mkdir()
        repeated_output.mkdir()
        second_output.mkdir()

        self._build("--wheel", output=first_output)
        self._build("--wheel", output=repeated_output)
        first = self._wheel_identity(next(first_output.glob("*.whl")))
        repeated = self._wheel_identity(next(repeated_output.glob("*.whl")))
        self.assertEqual(first, repeated)
        self.assertEqual(first["identity"]["revision"], self.baseline_revision)
        self.assertEqual(first["version"], first["metadataVersion"])
        self.assertEqual(first["version"], f"1.5.1+g{self.baseline_revision}")

        marker = self.repository / "src" / "omp_deck" / "_identity_marker.py"
        marker.write_text("SEQUENCE = 2\n", encoding="utf-8")
        self._run("git", "add", str(marker.relative_to(self.repository)))
        self._run("git", "commit", "-m", "next source revision")
        second_revision = self._run("git", "rev-parse", "HEAD").stdout.strip()
        self._build("--wheel", output=second_output)
        second = self._wheel_identity(next(second_output.glob("*.whl")))

        self.assertEqual(second["identity"]["revision"], second_revision)
        self.assertNotEqual(first["identity"], second["identity"])
        self.assertGreater(Version(second["version"]), Version(first["version"]))

    def test_tags_do_not_change_the_source_derived_identity(self):
        first_output = self.outputs / "before-tag"
        tagged_output = self.outputs / "after-tag"
        first_output.mkdir()
        tagged_output.mkdir()
        self._build("--wheel", output=first_output)
        first = self._wheel_identity(next(first_output.glob("*.whl")))

        self._run("git", "tag", "v999.0.0")
        self._build("--wheel", output=tagged_output)
        tagged = self._wheel_identity(next(tagged_output.glob("*.whl")))

        self.assertEqual(first, tagged)

    def test_dirty_or_untracked_source_cannot_build_an_artifact(self):
        (self.repository / "release-affecting.py").write_text("changed = True\n", encoding="utf-8")

        completed = self._build("--wheel", check=False)

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("artifact builds require a clean checkout", completed.stderr + completed.stdout)
        self.assertFalse(list(self.outputs.glob("*.whl")))

        (self.repository / "release-affecting.py").unlink()
        readme = self.repository / "README.md"
        original_readme = readme.read_text(encoding="utf-8")
        readme.write_text(original_readme + "\nchanged\n", encoding="utf-8")
        tracked = self._build("--wheel", check=False)
        self.assertNotEqual(tracked.returncode, 0)
        self.assertIn("artifact builds require a clean checkout", tracked.stderr + tracked.stdout)
        self.assertFalse(list(self.outputs.glob("*.whl")))

        readme.write_text(original_readme, encoding="utf-8")
        exclude = self.repository / ".git" / "info" / "exclude"
        exclude.write_text(
            "src/omp_deck/_ignored_payload.py\nsrc/omp_deck/static/ignored-payload.js\n",
            encoding="utf-8",
        )
        (self.repository / "src" / "omp_deck" / "_ignored_payload.py").write_text(
            "IGNORED = True\n",
            encoding="utf-8",
        )
        (self.repository / "src" / "omp_deck" / "static" / "ignored-payload.js").write_text(
            "window.ignoredPayload = true;\n",
            encoding="utf-8",
        )
        ignored = self._build("--wheel", check=False)
        self.assertNotEqual(ignored.returncode, 0)
        self.assertIn("untracked or ignored package files", ignored.stderr + ignored.stdout)
        self.assertFalse(list(self.outputs.glob("*.whl")))

    def test_stale_build_output_is_not_included_in_a_new_wheel(self):
        stale = self.repository / "build" / "lib" / "omp_deck" / "removed_module.py"
        stale.parent.mkdir(parents=True)
        stale.write_text("STALE = True\n", encoding="utf-8")
        self._build("--wheel")
        with zipfile.ZipFile(next(self.outputs.glob("*.whl"))) as wheel:
            self.assertNotIn("omp_deck/removed_module.py", wheel.namelist())

    def test_symlink_source_cannot_claim_an_immutable_revision(self):
        external = self.outputs / "external.py"
        external.write_text("VALUE = 1\n", encoding="utf-8")
        alias = self.repository / "src" / "omp_deck" / "external.py"
        try:
            alias.symlink_to(external)
        except OSError as error:
            self.skipTest(f"symlinks unavailable: {error}")
        self._run("git", "add", str(alias.relative_to(self.repository)))
        self._run("git", "commit", "-m", "linked source")
        result = self._build("--wheel", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("symlink", result.stdout + result.stderr)
        self.assertFalse(list(self.outputs.glob("*.whl")))

    def test_git_hidden_source_edits_cannot_reuse_a_committed_identity(self):
        relative = "src/omp_deck/__init__.py"
        self._run("git", "update-index", "--assume-unchanged", relative)
        with (self.repository / relative).open("a", encoding="utf-8") as source:
            source.write("\nUNCOMMITTED_CODE = True\n")

        result = self._build("--wheel", check=False)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("differs from committed revision", result.stdout + result.stderr)
        self.assertFalse(list(self.outputs.glob("*.whl")))

    def test_prepared_metadata_is_reusable_only_for_its_source_revision(self):
        metadata = self.outputs / "metadata"
        metadata.mkdir()
        prepared = self._run(
            sys.executable,
            "-c",
            "import _build_backend as b,sys; print(b.prepare_metadata_for_build_wheel(sys.argv[1]))",
            str(metadata),
        ).stdout.splitlines()[-1]
        command = (
            sys.executable,
            "-c",
            "import _build_backend as b,sys; b.build_wheel(sys.argv[1], metadata_directory=sys.argv[2])",
        )
        matching = self.outputs / "matching"
        matching.mkdir()
        self._run(*command, str(matching), str(metadata / prepared))
        identity = self._wheel_identity(next(matching.glob("*.whl")))
        self.assertEqual(identity["version"], identity["metadataVersion"])
        self.assertEqual(identity["identity"]["revision"], self.baseline_revision)

        marker = self.repository / "src/omp_deck/_marker.py"
        marker.write_text("NEW_SOURCE = True\n", encoding="utf-8")
        self._run("git", "add", str(marker.relative_to(self.repository)))
        self._run("git", "commit", "-m", "change source after metadata preparation")
        stale = self.outputs / "stale"
        stale.mkdir()
        rejected = self._run(*command, str(stale), str(metadata / prepared), check=False)

        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("prepared wheel metadata does not match", rejected.stdout + rejected.stderr)
        self.assertFalse(list(stale.glob("*.whl")))

    def test_missing_or_overridden_scm_identity_is_rejected(self):
        exported = Path(self.temporary.name) / "unidentified-export"
        shutil.copytree(self.repository, exported, ignore=shutil.ignore_patterns(".git"))
        missing = self._build("--wheel", cwd=exported, check=False)
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("without a complete Git checkout", missing.stderr + missing.stdout)

        environment = {**os.environ, "SETUPTOOLS_SCM_PRETEND_VERSION_FOR_OMP_DECK": "99"}
        overridden = self._build("--wheel", check=False, env=environment)
        self.assertNotEqual(overridden.returncode, 0)
        self.assertIn("identity overrides are forbidden", overridden.stderr + overridden.stdout)

        backend = self.repository / "_build_backend.py"
        configured = backend.read_text(encoding="utf-8").replace(
            self.version_baseline_revision,
            "0" * 40,
        )
        backend.write_text(configured, encoding="utf-8")
        self._run("git", "add", "_build_backend.py")
        self._run("git", "commit", "-m", "invalid version baseline")
        invalid_baseline = self._build("--wheel", check=False)
        self.assertNotEqual(invalid_baseline.returncode, 0)
        self.assertIn("baseline", invalid_baseline.stderr + invalid_baseline.stdout)

    def test_wheel_built_from_sdist_retains_exact_source_identity(self):
        sdist_output = self.outputs / "sdist"
        wheel_output = self.outputs / "wheel"
        extraction = self.outputs / "extracted"
        sdist_output.mkdir()
        wheel_output.mkdir()
        extraction.mkdir()
        self._build("--sdist", output=sdist_output)
        source_archive = next(sdist_output.glob("*.tar.gz"))
        with tarfile.open(source_archive, "r:gz") as archive:
            archive.extractall(extraction, filter="data")
        extracted_source = next(extraction.iterdir())

        self._build("--wheel", cwd=extracted_source, output=wheel_output)
        identity = self._wheel_identity(next(wheel_output.glob("*.whl")))

        self.assertEqual(identity["identity"]["revision"], self.baseline_revision)
        self.assertEqual(identity["version"], identity["metadataVersion"])

        tampered_output = self.outputs / "tampered-wheel"
        tampered_output.mkdir()
        source = extracted_source / "src" / "omp_deck" / "__init__.py"
        source.write_text(source.read_text(encoding="utf-8") + "\nTAMPERED = True\n", encoding="utf-8")
        tampered = self._build("--wheel", cwd=extracted_source, output=tampered_output, check=False)
        self.assertNotEqual(tampered.returncode, 0)
        self.assertIn("source distribution content changed", tampered.stderr + tampered.stdout)
        self.assertFalse(list(tampered_output.glob("*.whl")))


class SourceIdentityTests(unittest.TestCase):
    def test_unbuilt_checkout_reports_explicitly_unavailable_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            package = source / "omp_deck"
            package.mkdir()
            shutil.copy2(ROOT / "src" / "omp_deck" / "__init__.py", package / "__init__.py")
            script = (
                "import json, sys; "
                "sys.path.insert(0, sys.argv[1]); "
                "import omp_deck; "
                "print(json.dumps(omp_deck.build_identity()))"
            )
            completed = subprocess.run(
                [sys.executable, "-I", "-c", script, str(source)],
                check=True,
                capture_output=True,
                text=True,
            )
        identity = json.loads(completed.stdout)
        self.assertEqual(identity, {"version": "0+unbuilt", "revision": "unavailable"})


if __name__ == "__main__":
    unittest.main()
