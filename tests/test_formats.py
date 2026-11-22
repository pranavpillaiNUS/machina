import importlib.util
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
