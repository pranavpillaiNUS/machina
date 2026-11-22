import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from machina_deid.formats import rewrite_file


class FormatTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)

    def rewrite(self, extension, data, replacements):
        source = self.folder / ("source" + extension)
        destination = self.folder / ("destination" + extension)
        source.write_bytes(data)

        def replace(value):
            for before, after in replacements.items():
                value = value.replace(before, after)
            return value

        self.assertTrue(rewrite_file(source, destination, replace))
        self.assertEqual(source.read_bytes(), data)
        return destination.read_bytes()

    def test_step_rewrites_strings_but_keeps_schema_and_coordinates(self):
        data = (
            b"ISO-10303-21;\nHEADER;\nFILE_SCHEMA(('ACME'));\nENDSEC;\nDATA;\n"
            b"#1=CARTESIAN_POINT('ACME''s Br\\X2\\00E4\\X0\\cket',(2.5,3.0,4.0));\n"
            b"ENDSEC;\nEND-ISO-10303-21;\n"
        )
        updated = self.rewrite(".STEP", data, {"ACME's Bräcket": "NOVA's Bracket", "ACME": "NOVA"})
        self.assertIn(b"FILE_SCHEMA(('ACME'));", updated)
        self.assertIn(b"#1=CARTESIAN_POINT('NOVA''s Bracket',(2.5,3.0,4.0));", updated)

    def test_dxf_rewrites_labels_and_xrefs_without_touching_handles(self):
        data = (
            b"  0\r\nTEXT\r\n  1\r\nACME\r\n  5\r\nACME\r\n 10\r\n25.00\r\n"
            b"  0\r\nBLOCK\r\n  1\r\nC:\\Drawings\\ACME.dwg\r\n"
            b"  0\r\nMTEXT\r\n  1\r\n{\\fACME;ACME}\\P3.50 mm\r\n  0\r\nEOF\r\n"
        )
        updated = self.rewrite(".dxf", data, {"ACME": "NOVA"})
        self.assertEqual(updated, (
            data.replace(b"ACME\r\n  5", b"NOVA\r\n  5")
            .replace(b"ACME.dwg", b"NOVA.dwg").replace(b";ACME}", b";NOVA}")
        ))

    def test_iges_updates_hollerith_lengths_without_shifting_section_columns(self):
        def record(payload, section):
            return payload.ljust(72) + section + b"      1\r\n"

        data = (
            record(b"ACME CAD export", b"S")
            + record(b"1H,,1H;,4HACME,4HACME,3HCAD,1Hx,16,38,6,308,15,4HACME,1.0,2,2HMM;", b"G")
            + record(b"     116       1", b"D")
            + record(b"116,1.0,2.0,3.0;", b"P")
            + record(b"S      1G      1D      1P      1", b"T")
        )
        updated = self.rewrite(".igs", data, {"ACME": "NOVA CO"})
        lines = updated.splitlines()
        metadata = b"".join(line[:72] for line in lines if line[72:73] == b"G")
        self.assertIn(b"7HNOVA CO,7HNOVA CO", metadata)
        self.assertIn(b"7HNOVA CO,1.0,2,2HMM;", metadata)
        self.assertEqual(lines[3:5], data.splitlines()[2:4])
        self.assertTrue(lines[-1].startswith(b"S      1G      2D      1P      1"))
        self.assertTrue(all(len(line) == 80 for line in lines))

    @unittest.skipUnless(importlib.util.find_spec("pymupdf"), "Install PyMuPDF for PDF adapter tests")
    def test_pdf_rewrites_visible_metadata_and_rotated_text_preserving_vectors(self):
        import pymupdf

        source = self.folder / "source.pdf"
        with pymupdf.open() as document:
            page = document.new_page()
            page.insert_text((70, 72), "PROJECT: ACME 3.50 mm")
            page.insert_text((300, 100), "ACME", rotate=90)
            page.draw_line((65, 70), (240, 70))
            original_drawing = page.get_drawings()[0]["items"]
            annotation = page.add_text_annot((180, 130), "ACME note")
            annotation.set_info(title="ACME")
            document.set_metadata({"author": "ACME"})
            document.set_xml_metadata(
                '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
                '<dc:creator xmlns:dc="http://purl.org/dc/elements/1.1/">ACME</dc:creator>'
                '</x:xmpmeta>'
            )
            document.save(source)
        output = self.rewrite(".pdf", source.read_bytes(), {"ACME": "NOVA"})
        with pymupdf.open(stream=output, filetype="pdf") as rewritten:
            page = rewritten[0]
            visible = page.get_text()
            self.assertIn("3.50 mm", visible)
            self.assertNotIn("ACME", visible)
            self.assertEqual(visible.count("NOVA"), 2)
            self.assertEqual(page.get_drawings()[0]["items"], original_drawing)
            self.assertEqual(next(page.annots()).info["content"], "NOVA note")
            self.assertEqual(rewritten.metadata["author"], "NOVA")
            self.assertIn("NOVA", rewritten.get_xml_metadata())

    @unittest.skipUnless(importlib.util.find_spec("pymupdf"), "Install PyMuPDF for PDF adapter tests")
    def test_pdf_file_specs_preserve_shared_annotation_links_forms_and_opaque_data(self):
        import pymupdf

        source = self.folder / "source.pdf"
        output = self.folder / "output.pdf"
        with pymupdf.open() as document:
            page = document.new_page()
            page.draw_line((50, 60), (150, 60))
            drawing = page.get_drawings()[0]["items"]
            widget = pymupdf.Widget()
            widget.field_name = "Dimension"
            widget.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
            widget.field_value = "3.50 mm"
            widget.rect = pymupdf.Rect(20, 100, 200, 120)
            page.add_widget(widget)

            file = page.add_file_annot((50, 25), b"authors: ACME", "ACME.txt", desc="ACME drawing note")
            file_xref = file.xref
            filespec = document.get_new_xref()
            document.update_object(filespec, document.xref_get_key(file_xref, "FS")[1])
            document.xref_set_key(filespec, "AFRelationship", "/Supplement")
            document.xref_set_key(file_xref, "FS", f"{filespec} 0 R")
            document.xref_set_key(page.xref, "AF", f"[{filespec} 0 R]")
            document.xref_set_key(document.pdf_catalog(), "Names", (
                f"<< /EmbeddedFiles << /Names [(ACME.txt) {filespec} 0 R] >> >>"
            ))
            document.embfile_add("opaque.cad", b"opaque data\0", desc="ACME model")
            document.save(source)

        calls = []

        def transform(name, staged, destination):
            calls.append(name)
            if staged.suffix != ".txt":
                return False
            self.assertEqual(destination.suffix, ".txt")
            destination.write_bytes(staged.read_bytes().replace(b"ACME", b"NOVATEK"))
            return True

        self.assertTrue(rewrite_file(source, output, lambda text: text.replace("ACME", "NOVATEK"),
                                     attachment_transform=transform))
        self.assertCountEqual(calls, ["ACME.txt", "opaque.cad"])
        with pymupdf.open(output) as document:
            page = document[0]
            self.assertEqual(page.get_drawings()[0]["items"], drawing)
            self.assertEqual(next(page.widgets()).field_value, "3.50 mm")
            attachment = next(page.annots(types=[pymupdf.PDF_ANNOT_FILE_ATTACHMENT]))
            self.assertEqual(attachment.file_info["filename"], "NOVATEK.txt")
            self.assertEqual(attachment.file_info["description"], "NOVATEK drawing note")
            self.assertEqual(attachment.get_file(), b"authors: NOVATEK")
            self.assertEqual(document.embfile_get("NOVATEK.txt"), b"authors: NOVATEK")
            self.assertEqual(document.embfile_get("opaque.cad"), b"opaque data\0")
            self.assertEqual(document.embfile_info("opaque.cad")["description"], "NOVATEK model")
            specification = document.xref_get_key(attachment.xref, "FS")[1]
            self.assertIn(specification, document.xref_get_key(page.xref, "AF")[1])
            self.assertEqual(document.xref_get_key(int(specification.split()[0]), "AFRelationship")[1], "/Supplement")

    @unittest.skipUnless(importlib.util.find_spec("pymupdf"), "Install PyMuPDF for PDF adapter tests")
    def test_pdf_gotoe_name_resolution_and_oversized_payload_are_preserved(self):
        import pymupdf

        source = self.folder / "source.pdf"
        output = self.folder / "output.pdf"
        payload = b"ACME" * 1024
        with pymupdf.open() as document:
            document.new_page()
            attachment = document.embfile_add("ACME.txt", payload, filename="ACME.txt", desc="ACME note")
            document.xref_set_key(attachment, "Params/Size", "2")
            document.xref_set_key(document.pdf_catalog(), "OpenAction", (
                "<< /S /GoToE /D [0 /Fit] /T << /R /C /N (ACME.txt) >> >>"
            ))
            document.save(source)

        def transform(*_args):
            self.fail("Oversized attachment should stay opaque")

        with patch("machina_deid.formats._PDF_ATTACHMENT_LIMIT", 32):
            self.assertTrue(rewrite_file(source, output, lambda text: text.replace("ACME", "NOVATEK"),
                                         attachment_transform=transform))
        with pymupdf.open(output) as document:
            self.assertEqual(document.embfile_names(), ["ACME.txt"])
            self.assertEqual(document.embfile_info("ACME.txt")["filename"], "ACME.txt")
            self.assertEqual(document.embfile_info("ACME.txt")["description"], "NOVATEK note")
            self.assertEqual(document.embfile_get("ACME.txt"), payload)
            self.assertEqual(document.xref_get_key(document.pdf_catalog(), "OpenAction/T/N")[1], "ACME.txt")

    @unittest.skipUnless(importlib.util.find_spec("pymupdf"), "Install PyMuPDF for PDF adapter tests")
    def test_pdf_multilevel_attachment_name_tree_keeps_limits_and_specs(self):
        import pymupdf

        source = self.folder / "source.pdf"
        output = self.folder / "output.pdf"
        with pymupdf.open() as document:
            document.new_page()
            first = document.embfile_add("ACME.txt", b"first")
            second = document.embfile_add("zz.txt", b"second")
            leaves = []
            for name, stream in (("ACME.txt", first), ("zz.txt", second)):
                leaf = document.get_new_xref()
                document.update_object(leaf, (
                    f"<< /Limits [({name}) ({name})] /Names [({name}) "
                    f"<< /Type /Filespec /F ({name}) /EF << /F {stream} 0 R >> >>] >>"
                ))
                leaves.append(leaf)
            document.xref_set_key(document.pdf_catalog(), "Names/EmbeddedFiles", (
                f"<< /Kids [{leaves[0]} 0 R {leaves[1]} 0 R] >>"
            ))
            document.save(source)
        self.assertTrue(rewrite_file(source, output, lambda text: text.replace("ACME", "NOVA")))
        with pymupdf.open(output) as document:
            kinds = [document.xref_get_key(index, "Limits") for index in range(1, document.xref_length())]
            self.assertIn(("array", "[(NOVA.txt)(NOVA.txt)]"), kinds)
            self.assertIn(("array", "[(zz.txt)(zz.txt)]"), kinds)
            self.assertIn(b"first", [document.xref_stream(index) for index in range(1, document.xref_length())])
            self.assertIn(b"second", [document.xref_stream(index) for index in range(1, document.xref_length())])


if __name__ == "__main__":
    unittest.main()
