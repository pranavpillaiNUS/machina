import codecs
import gzip
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from machina_deid.svg import rewrite_svg


class SvgTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)

    def set_source(self, suffix, payload):
        source = self.folder / ("input" + suffix)
        destination = self.folder / ("output" + suffix)
        source.write_bytes(payload)
        return source, destination

    def test_text_metadata_and_links_preserve_original_xml_outside_exact_edits(self):
        original = (
            b'<?xml version="1.0"?>\r\n'
            b'<!DOCTYPE p:svg SYSTEM "https://example.invalid/unused.dtd">\r\n'
            b'<p:svg xmlns:p="http://www.w3.org/2000/svg" xmlns:v="http://www.w3.org/1999/xlink" '
            b'xmlns:d="http://purl.org/dc/elements/1.1/">\r\n'
            b'<p:path d="M0 0 10 6" id="ACME" style="fill:url(#ACME)"/>\r\n'
            b'<p:title>ACME &amp; Co</p:title><p:desc><![CDATA[Drawn by ACME]]></p:desc>\r\n'
            b'<p:text x="4" y="8"><p:tspan>ACME</p:tspan> 3.50 mm</p:text>\r\n'
            b'<p:metadata><d:creator>ACME</d:creator></p:metadata>\r\n'
            b"<p:image  v:href='../ACME%20Co.png#ACME'/><p:use href=\"#ACME\"/>\r\n"
            b'<p:image href="data:image/png;base64,ACME"/>\r\n'
            b'</p:svg>\r\n'
        )
        source, destination = self.set_source(".svg", original)
        self.assertTrue(rewrite_svg(source, destination, lambda text: text.replace("ACME", "NOVA")))
        expected = (
            original.replace(b">ACME", b">NOVA")
            .replace(b"Drawn by ACME", b"Drawn by NOVA")
            .replace(b"../ACME%20Co.png", b"../NOVA%20Co.png")
        )
        self.assertEqual(destination.read_bytes(), expected)
        self.assertEqual(source.read_bytes(), original)

    def test_utf16_cdata_and_quoted_attribute_keep_encoding_and_remain_valid(self):
        original = (
            '<?xml version="1.0" encoding="UTF-16"?>'
            '<svg><title><![CDATA[ACME]]></title><g aria-label="ACME"/></svg>'
        )
        source, destination = self.set_source(".svg", codecs.BOM_UTF16_LE + original.encode("utf-16-le"))
        replacement = 'Søren & "Co" ]]>'
        self.assertTrue(rewrite_svg(source, destination, lambda text: replacement if text == "ACME" else text))
        data = destination.read_bytes()
        self.assertTrue(data.startswith(codecs.BOM_UTF16_LE))
        self.assertIn("Søren & \"Co\" ]]]]><![CDATA[>", data.decode("utf-16"))
        self.assertIn('aria-label="Søren &amp; &quot;Co&quot; ]]&gt;"', data.decode("utf-16"))

    def test_gzip_rewrite_is_deterministic_and_bounds_expansion(self):
        payload = b'<svg><text>ACME</text></svg>'
        source, destination = self.set_source(".svgz", gzip.compress(payload))
        replace = lambda value: value.replace("ACME", "NOVA")
        self.assertTrue(rewrite_svg(source, destination, replace))
        first = destination.read_bytes()
        self.assertTrue(rewrite_svg(source, destination, replace))
        self.assertEqual(destination.read_bytes(), first)
        self.assertEqual(gzip.decompress(first), payload.replace(b"ACME", b"NOVA"))

        destination.unlink()
        with patch("machina_deid.svg.MAX_SVG_BYTES", 12), self.assertRaisesRegex(ValueError, "document size"):
            rewrite_svg(source, destination, replace)
        self.assertFalse(destination.exists())

    def test_entities_and_invalid_xml_fail_before_opening_destination(self):
        documents = [
            b'<!DOCTYPE svg [<!ENTITY name SYSTEM "file:///missing">]><svg><text>&name;</text></svg>',
            b'<svg><text>Acme</svg>',
        ]
        for document in documents:
            with self.subTest(document=document):
                source, destination = self.set_source(".svg", document)
                with self.assertRaises(ValueError):
                    rewrite_svg(source, destination, lambda value: value)
                self.assertFalse(destination.exists())

    def test_unchanged_xml_leaves_destination_for_caller(self):
        source, destination = self.set_source(".svg", b'<svg><use href="#ACME" id="ACME"/></svg>')
        self.assertFalse(rewrite_svg(source, destination, lambda text: text.replace("ACME", "NOVA")))
        self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
