import struct
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from machina_deid.geometry_text import rewrite_geometry_text


class GeometryMetadataTests(unittest.TestCase):
    def rewrite(self, suffix, raw, rename=lambda value: value.replace("Acme", "Cedar Works")):
        with TemporaryDirectory() as folder:
            source = Path(folder) / f"source{suffix}"
            destination = Path(folder) / f"mapped{suffix}"
            source.write_bytes(raw)
            changed = rewrite_geometry_text(source, destination, rename)
            return destination.read_bytes() if changed else None

    def test_acis_updates_only_the_length_prefixed_product_id(self):
        geometry = b"-0 body $1 $2 $-1 $-1 #\r\n-1 unknown-attrb $0 @4 Acme #\r\nEnd-of-ACIS-data\r\n"
        for prefix in (b"", b"@"):
            with self.subTest(length_prefix=prefix):
                metadata = (
                    prefix + b"4 Acme " + prefix + b"11 ACIS 7.0 NT "
                    + prefix + b"24 Mon Apr 09 16:44:18 2001\r\n"
                )
                header = b"712 0 1 0\r\n" + metadata + b"-1 9.999999999e-07 1e-10\r\n"
                self.assertEqual(
                    self.rewrite(".sat", header + geometry),
                    header.replace(b"4 Acme", b"11 Cedar Works", 1) + geometry,
                )

    def test_acis_copies_unrecognized_header_dialects(self):
        self.assertIsNone(self.rewrite(".sat", b"712 0 1 0\n500 Acme\n-1 9e-07 1e-10\n"))

    def test_acis_rewrites_known_named_attributes_without_changing_keys(self):
        record = b"string_attrib-name_attrib-gen-attrib $-1 -1 $-1 $-1 $0 keep keep_kept ignore copy "
        original = (
            b"712 0 1 0\n8 Exporter 11 ACIS 7.0 NT 24 Mon Apr 09 16:44:18 2001\n-1 9e-07 1e-10\n"
            b"-0 body $1 -1 $-1 $-1 $-1 $-1 #\n"
            b"-1 " + record + b"@17 ATTRIB_XACIS_NAME @4 Acme #\n"
            b"-2 " + record + b"@8 MATERIAL @4 Acme #\n"
            b"End-of-ACIS-data\n"
        )
        self.assertEqual(
            self.rewrite(".sat", original),
            original.replace(b"@17 ATTRIB_XACIS_NAME @4 Acme", b"@17 ATTRIB_XACIS_NAME @11 Cedar Works"),
        )

    def test_binary_stl_preserves_facet_data_and_color_extension_bytes(self):
        color = b"COLOR=\xfe\x08\x12\xff;MATERIAL=" + b"\xfe\x08\x12\xff" * 3
        header = b"Acme Widget".ljust(35, b"\x00") + color.ljust(45, b"\x00")
        facets = struct.pack("<I12fH", 1, *[0.25] * 12, 0xDA20)
        changed = self.rewrite(".stl", header + facets)
        self.assertTrue(changed.startswith(b"Cedar Works Widget\x00"))
        self.assertEqual(changed[35:], header[35:] + facets)

    def test_binary_stl_rejects_pseudonyms_that_do_not_fit(self):
        raw = b"Acme".ljust(80, b"\x00") + struct.pack("<I", 0)
        self.assertIsNone(self.rewrite(".stl", raw, lambda _: "x" * 90))

    def test_ascii_stl_only_changes_solid_names(self):
        facet = (
            b"  facet normal 0 0 1\r\n    outer loop\r\n      vertex 0 0 0\r\n"
            b"      vertex 0 1 0\r\n      vertex 1 0 0\r\n    endloop\r\n  endfacet\r\n"
        )
        raw = b"solid Acme\r\n" + facet + b"endsolid Acme\r\n"
        self.assertEqual(self.rewrite(".stl", raw), raw.replace(b"Acme", b"Cedar Works"))
        self.assertIsNone(self.rewrite(".stl", b"solid Acme\r\n" + facet))

    def test_vrml_rewrites_typed_text_and_urls_preserving_geometry_and_names(self):
        original = (
            b"#VRML V2.0 utf8\n# Acme geometry\n"
            b'DEF Acme WorldInfo { title "Acme" info [ "Acme author", "ACME" ] }\n'
            b'Group { children [ Inline { url [ "Acme.wrl" ] } '
            b'Text { string [ "Acme # bracket [ and \\"person\\"" ] } ] }\n'
            b'Script { url [ "javascript: Acme()" ] }\n'
            b"USE Acme\n"
        )
        changed = self.rewrite(".wrl", original)
        self.assertEqual(changed, (
            b"#VRML V2.0 utf8\n# Acme geometry\n"
            b'DEF Acme WorldInfo { title "Cedar Works" info [ "Cedar Works author", "ACME" ] }\n'
            b'Group { children [ Inline { url [ "Cedar Works.wrl" ] } '
            b'Text { string [ "Cedar Works # bracket [ and \\"person\\"" ] } ] }\n'
            b'Script { url [ "javascript: Acme()" ] }\n'
            b"USE Acme\n"
        ))

    def test_vrml_does_not_publish_edits_when_structure_is_unbalanced(self):
        self.assertIsNone(self.rewrite(".wrl", b'#VRML V2.0 utf8\nWorldInfo { title "Acme"'))


if __name__ == "__main__":
    unittest.main()
