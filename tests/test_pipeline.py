"""Integration checks for staged corpus processing."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZipFile

from machina_deid.pipeline import RunOptions, rewrite_corpus
from machina_deid.policy import Rulebook


STEP = (
    b"ISO-10303-21;\nHEADER;\n"
    b"FILE_DESCRIPTION(('Example Foundry'), '2;1');\n"
    b"FILE_NAME('Example Foundry.step','',('Author'),('Example Foundry'),'','','');\n"
    b"FILE_SCHEMA(('AUTOMOTIVE_DESIGN'));\nENDSEC;\nDATA;\n"
    b"#1=CARTESIAN_POINT('',(1.0,2.0,3.0));\nENDSEC;\nEND-ISO-10303-21;\n"
)
RULES = Rulebook.from_dict(
    {"rules": [{"source": "Example Foundry", "replacement": "Juniper Works"}]}
)


class PipelineTests(unittest.TestCase):
    def test_rewrites_content_and_preserves_geometry_and_paths_by_default(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input"
            source.mkdir()
            (source / "Example Foundry.step").write_bytes(STEP)
            (source / "native.ipt").write_bytes(b"unhandled native bytes")
            (source / "empty.pdf").touch()
            (source / ".get-input").mkdir()
            (source / ".get-input" / "manifest.tsv").touch()

            corpus = root / "output" / "corpus"
            report = rewrite_corpus(source, corpus, RULES)

            rewritten = (corpus / "Example Foundry.step").read_bytes()
            self.assertIn(b"Juniper Works", rewritten)
            self.assertIn(b"'Example Foundry.step'", rewritten)
            self.assertIn(b"#1=CARTESIAN_POINT('',(1.0,2.0,3.0));", rewritten)
            self.assertEqual((corpus / "native.ipt").read_bytes(), b"unhandled native bytes")
            self.assertFalse((corpus / ".get-input").exists())
            self.assertEqual(report["by_status"], {"empty": 1, "rewritten": 1, "unsupported": 1})
            self.assertEqual(report["renamed"], 0)

    def test_renames_file_and_known_reference_together_when_enabled(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input"
            source.mkdir()
            (source / "Example Foundry.step").write_bytes(STEP)
            corpus = root / "corpus"
            report = rewrite_corpus(source, corpus, RULES, RunOptions(rename_paths=True))

            self.assertEqual(report["renamed"], 1)
            self.assertIn(b"'Juniper Works.step'", (corpus / "Juniper Works.step").read_bytes())
            self.assertEqual((source / "Example Foundry.step").read_bytes(), STEP)

    def test_failure_leaves_no_output_unless_copy_for_review_was_requested(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input"
            source.mkdir()
            (source / "broken.step").write_bytes(b"damaged")
            corpus = root / "corpus"

            with self.assertRaisesRegex(ValueError, "adapter failed"):
                rewrite_corpus(source, corpus, RULES)
            self.assertFalse(corpus.exists())
            report = rewrite_corpus(source, corpus, RULES, RunOptions(preserve_on_error=True))
            self.assertEqual(report["by_status"], {"adapter_failed": 1})
            self.assertEqual((corpus / "broken.step").read_bytes(), b"damaged")
            with self.assertRaisesRegex(ValueError, "Output already exists"):
                rewrite_corpus(source, corpus, RULES)

    def test_archives_use_separate_reference_namespaces_and_report_leaf_coverage(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input"
            source.mkdir()
            with ZipFile(source / "drawings.zip", "w") as archive:
                archive.writestr("bundle/Example Foundry.step", STEP)
                archive.writestr("bundle/model.ipt", b"opaque native payload")

            corpus = root / "corpus"
            report = rewrite_corpus(source, corpus, RULES, RunOptions(rename_paths=True))

            self.assertEqual(report["by_status"], {"rewritten": 1})
            self.assertEqual(report["archive_member_status"], {"rewritten": 1, "unsupported": 1})
            with ZipFile(corpus / "drawings.zip") as archive:
                rewritten = archive.read("bundle/Example Foundry.step")
                self.assertIn(b"'Example Foundry.step'", rewritten)
                self.assertIn(b"Juniper Works", rewritten)
                self.assertEqual(archive.read("bundle/model.ipt"), b"opaque native payload")
                self.assertIsNone(archive.testzip())

    def test_copy_for_review_is_scoped_to_the_failed_archive_member(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input"
            source.mkdir()
            with ZipFile(source / "drawings.zip", "w") as archive:
                archive.writestr("good.step", STEP)
                archive.writestr("broken.step", b"damaged")

            corpus = root / "corpus"
            with self.assertRaisesRegex(ValueError, "adapter failed"):
                rewrite_corpus(source, corpus, RULES)
            report = rewrite_corpus(source, corpus, RULES, RunOptions(preserve_on_error=True))
            self.assertEqual(report["archive_member_status"], {"adapter_failed": 1, "rewritten": 1})
            with ZipFile(corpus / "drawings.zip") as archive:
                self.assertIn(b"Juniper Works", archive.read("good.step"))
                self.assertEqual(archive.read("broken.step"), b"damaged")


if __name__ == "__main__":
    unittest.main()
