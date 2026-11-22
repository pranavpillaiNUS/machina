import json
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from machina_deid.attempt import prepare_attempt


class AttemptPreparationTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "--quiet")
        self.git("config", "user.name", "Fixture Developer")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "core.autocrlf", "false")
        self.git("config", "commit.gpgsign", "false")
        self.write_tracked("machina_deid/__init__.py", b"FIRST = True\n")
        self.write_tracked("tests/test_example.py", b"# Verification\n")
        self.write_tracked("README.md", b"# Code revision fixture\n")
        self.write_tracked("LICENSE", b"MIT fixture\n")
        self.write_tracked("pyproject.toml", b'[project.optional-dependencies]\npdf = ["PyMuPDF==1.26.7"]\n')
        self.write_tracked("private/rules.json", b"synthetic private fixture")
        self.git("commit", "-qm", "Fixture snapshot")
        self.revision = self.git("rev-parse", "HEAD")

        self.attempt = self.root / "attempt-1"
        self.corpus = self.attempt / "corpus"
        self.corpus.mkdir(parents=True)
        (self.corpus / "part.step").write_bytes(b"model bytes")
        self.report = self.root / "report.json"
        self.write_report()

    def git(self, *arguments):
        return subprocess.check_output(
            ["git", "-C", str(self.repo), *arguments], stderr=subprocess.STDOUT, text=True,
        ).strip()

    def write_tracked(self, name, content):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        self.git("add", "--", name)

    def write_report(self, *, encoding="utf-8", **changes):
        report = {
            "files": 1,
            "output_bytes": 11,
            "renamed": 1,
            "run_options": {
                "workers": 3,
                "max_edit_mib": 48.0,
                "rename_paths": True,
                "preserve_on_error": False,
            },
            "by_status": {"rewritten": 1},
            "by_suffix": {".step": {"rewritten": 1}},
            "archive_member_status": {"source_pointer": 2, "unchanged": 3},
            "private_rule_names": ["synthetic private fixture"],
        }
        report.update(changes)
        self.report.write_text(json.dumps(report), encoding=encoding)

    def test_packages_only_the_named_revision_and_public_aggregate_report(self):
        self.write_tracked("machina_deid/__init__.py", b"SECOND = True\n")
        self.git("commit", "-qm", "Second fixture snapshot")
        (self.repo / "machina_deid" / "__init__.py").write_bytes(b"WORKTREE = True\n")
        (self.repo / "machina_deid" / "ignored.py").write_bytes(b"# Untracked fixture\n")
        self.write_report(encoding="utf-16", revision=self.revision)

        with patch("machina_deid.attempt.SCORER_FILE_BYTES", 10):
            result = prepare_attempt(self.attempt, self.repo, self.report, revision=self.revision)

        self.assertEqual(result, {
            "revision": self.revision,
            "corpus_files": 1,
            "corpus_bytes": 11,
            "files_over_2_gb": 1,
            "code_files": 5,
        })
        code = self.attempt / "code"
        self.assertEqual((code / "machina_deid" / "__init__.py").read_bytes(), b"FIRST = True\n")
        self.assertFalse((code / "machina_deid" / "ignored.py").exists())
        self.assertFalse((code / "private").exists())
        readme = (self.attempt / "README.txt").read_text(encoding="utf-8")
        self.assertIn("--workers 3 --max-edit-mib 48 --rename-paths", readme)
        self.assertIn("python -m pip install '.[pdf]'", readme)
        self.assertIn("Files above 10 bytes 1", readme)
        self.assertIn("unchanged 3", readme)
        self.assertIn("source_pointer 2", readme)
        self.assertNotIn("synthetic private fixture", readme)
        self.assertNotIn(str(self.report), readme)
        self.assertTrue((self.corpus / "part.step").exists())

    def test_rejects_mismatch_and_never_overwrites_existing_deliverables(self):
        self.write_report(output_bytes=12)
        with self.assertRaisesRegex(ValueError, "differs from the run report"):
            prepare_attempt(self.attempt, self.repo, self.report)
        self.assertFalse((self.attempt / "code").exists())
        self.assertFalse((self.attempt / "README.txt").exists())

        self.write_report()
        original = self.attempt / "README.txt"
        original.write_text("keep this", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "already contains"):
            prepare_attempt(self.attempt, self.repo, self.report)
        self.assertEqual(original.read_text(encoding="utf-8"), "keep this")

    def test_enforces_object_cap_and_handles_publish_errors(self):
        with patch("machina_deid.attempt.MAX_CORPUS_FILES", 0):
            with self.assertRaisesRegex(ValueError, "object limit"):
                prepare_attempt(self.attempt, self.repo, self.report)
        with patch("machina_deid.attempt.Path.rename", side_effect=OSError("synthetic publication failure")):
            with self.assertRaises(OSError):
                prepare_attempt(self.attempt, self.repo, self.report)
        self.assertEqual(list(self.attempt.iterdir()), [self.corpus])
        prepare_attempt(self.attempt, self.repo, self.report)

    def test_rejects_private_paths_inside_snapshot_roots(self):
        self.write_tracked("tests/private/rules.py", b"# Synthetic fixture outside the source allowlist\n")
        self.git("commit", "-qm", "Inadvertent private artifact")
        with self.assertRaisesRegex(ValueError, "source allowlist"):
            prepare_attempt(self.attempt, self.repo, self.report)
        self.assertEqual(list(self.attempt.iterdir()), [self.corpus])

    def test_fractional_mib_uses_exact_byte_limit(self):
        self.write_report(run_options={
            "workers": 2, "max_edit_mib": 0.125, "rename_paths": True, "preserve_on_error": False,
        })
        prepare_attempt(self.attempt, self.repo, self.report)
        readme = (self.attempt / "README.txt").read_text(encoding="utf-8")
        self.assertIn("max_edit_bytes=131072", readme)


if __name__ == "__main__":
    unittest.main()
