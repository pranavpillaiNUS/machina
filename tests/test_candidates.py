import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from machina_deid.candidates import STEP_HEADER_BYTES, discover_candidates


def step_file(author, organization):
    return (
        b"ISO-10303-21;\nHEADER;\n"
        b"FILE_DESCRIPTION(('value ignored@example.com'),'2;1');\n"
        b"/* FILE_NAME('ignored','',('ignored@example.com'),(''),'','',''); */\n"
        b"FILE_NAME('sample.step','2024-07-01T01:02:03'," + author + b"," + organization + b","
        b"'engine ignored@example.com','software','');\n"
        b"FILE_SCHEMA(('AUTOMOTIVE_DESIGN'));\nENDSEC;\nDATA;\n"
        b"#1=PRODUCT('Geometry Designer','ignored@example.com','',());\n"
        b"ENDSEC;\nEND-ISO-10303-21;\n"
    )


class CandidateDiscoveryTests(unittest.TestCase):
    def test_step_reports_typed_fields_and_apostrophe_and_unicode_escapes(self):
        with TemporaryDirectory() as folder:
            base = Path(folder)
            corpus = base / "input"
            corpus.mkdir()
            (corpus / "one.step").write_bytes(step_file(
                b"('Ana O''Brien <Ana+CAD@Example.com>','Ana O''Brien')",
                b"('C\\X2\\00E9\\X0\\dar Engineering','https://docs.example.org/')",
            ))
            (corpus / "two.stp").write_bytes(step_file(
                b"('ana o''brien <ana+cad@example.com>','Unknown')", b"('example.com')"
            ))
            output = base / "private.json"
            summary = discover_candidates(corpus, output)
            report = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(summary, report["summary"])
        self.assertEqual(summary["inspected"], {"step": 2})
        self.assertEqual(summary["by_kind"], {"author": 1, "domain": 2, "email": 1, "organization": 1})
        lookup = {(item["kind"], item["source"]): item for item in report["candidates"]}
        self.assertEqual(lookup["author", "Ana O'Brien"]["fields"], {"step.author": 2})
        self.assertEqual(lookup["email", "Ana+CAD@Example.com"]["aliases"], ["ana+cad@example.com"])
        self.assertEqual(lookup["domain", "Example.com"]["fields"], {"step.author": 2, "step.organization": 1})
        self.assertIn(("organization", "Cédar Engineering"), lookup)
        self.assertNotIn("Geometry Designer", json.dumps(report))
        self.assertNotIn("ignored@example.com", json.dumps(report))
        self.assertNotIn("Ana", json.dumps(summary))

    def test_step_ignores_late_header_records_and_data_section_lookalikes(self):
        with TemporaryDirectory() as folder:
            base = Path(folder)
            corpus = base / "input"
            corpus.mkdir()
            (corpus / "late.step").write_bytes(
                b"ISO-10303-21; HEADER; /*" + b"x" * STEP_HEADER_BYTES + b"*/"
                b"FILE_NAME('late','',('Reveal Me'),(''),'','','');"
            )
            (corpus / "data.step").write_bytes(
                b"ISO-10303-21; HEADER; ENDSEC; DATA;"
                b"FILE_NAME('fake','',('Reveal Me'),(''),'','','');"
            )
            result = discover_candidates(corpus, base / "review.json")

        self.assertEqual(result["candidate_count"], 0)
        self.assertEqual(result["skipped"], {"step_header_unavailable": 2})

    def test_review_refuses_existing_or_corpus_destinations(self):
        with TemporaryDirectory() as folder:
            base = Path(folder)
            corpus = base / "input"
            corpus.mkdir()
            review = base / "private.json"
            review.write_text("keep", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "already exists"):
                discover_candidates(corpus, review)
            with self.assertRaisesRegex(ValueError, "outside the input"):
                discover_candidates(corpus, corpus / "review.json")
            self.assertEqual(review.read_text(encoding="utf-8"), "keep")

    def test_pdf_reads_only_info_and_suppresses_parser_messages(self):
        try:
            import pymupdf
        except ImportError:
            self.skipTest("PDF extra is not installed")

        with TemporaryDirectory() as folder:
            base = Path(folder)
            corpus = base / "input"
            corpus.mkdir()
            with pymupdf.open() as document:
                page = document.new_page()
                page.insert_text((20, 40), "visible@example.org")
                document.set_metadata({
                    "title": "Project Blue", "author": "Ana O'Brien <ana@example.com>",
                    "subject": "unrelated@example.org", "creator": "Ignore Creator",
                })
                document.save(corpus / "source.pdf")
            (corpus / "bad.pdf").write_bytes(b"%PDF-1.7\ntruncated private bytes\n")
            output = base / "private.json"
            stdout, stderr = io.StringIO(), io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                result = discover_candidates(corpus, output)
            raw = output.read_text(encoding="utf-8")

        self.assertEqual(result["by_kind"], {"author": 1, "domain": 1, "email": 1, "title": 1})
        self.assertEqual(result["skipped"], {"pdf_info_unavailable": 1})
        self.assertEqual(stdout.getvalue() + stderr.getvalue(), "")
        self.assertIn("Project Blue", raw)
        self.assertNotIn("visible@example.org", raw)
        self.assertNotIn("unrelated@example.org", raw)


    def test_dxf_header_people_and_project_become_candidates(self):
        with TemporaryDirectory() as folder:
            base = Path(folder)
            corpus = base / "input"
            corpus.mkdir()
            (corpus / "plan.dxf").write_bytes(
                b"  0\nSECTION\n  2\nHEADER\n  9\n$AUTHOR\n  1\nAna Smith\n  9\n$LASTSAVEDBY\n  1\nasmith\n"
                b"  9\n$PROJECTNAME\n  1\nBlue Tower\n  0\nENDSEC\n  0\nEOF\n"
            )
            (corpus / "plan.dwg").write_bytes(b"AC1032\n")
            with mock.patch.dict(os.environ, {"MACHINA_ODA_CONVERTER": "", "PATH": ""}):
                summary = discover_candidates(corpus, base / "review.json")
            report = json.loads((base / "review.json").read_text(encoding="utf-8"))

        lookup = {(item["kind"], item["source"]): item for item in report["candidates"]}
        self.assertEqual(lookup["author", "Ana Smith"]["fields"], {"dxf.$AUTHOR": 1})
        self.assertEqual(lookup["author", "asmith"]["fields"], {"dxf.$LASTSAVEDBY": 1})
        self.assertIn(("project", "Blue Tower"), lookup)
        self.assertEqual(summary["inspected"], {"dxf": 1})
        self.assertEqual(summary["skipped"], {"dwg_converter_unavailable": 1})


if __name__ == "__main__":
    unittest.main()
