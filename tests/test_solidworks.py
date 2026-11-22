import unittest
import zlib
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from machina_deid.solidworks import (
    SolidWorksError,
    SolidWorksUnavailable,
    SolidWorksUnsupported,
    _render_verified,
    _rewrite_xml_metadata,
    is_modern_solidworks,
    read_identity_metadata,
    rewrite_solidworks,
)

CORE_NS = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
DC_NS = "http://purl.org/dc/elements/1.1/"
EXTENDED_NS = "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
CUSTOM_NS = "http://schemas.openxmlformats.org/officeDocument/2006/custom-properties"
CONFIG_NS = "http://www.solidworks.com/config-properties"
VALUE_NS = "http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"


class SolidWorksTests(unittest.TestCase):
    def test_probe_and_typed_identity_evidence(self):
        core = (
            f'<cp:coreProperties xmlns:cp="{CORE_NS}" xmlns:dc="{DC_NS}">'
            "<dc:creator>April Example</dc:creator>"
            "<dc:title>Engineering specification</dc:title></cp:coreProperties>"
        ).encode()
        app = (
            f'<Properties xmlns="{EXTENDED_NS}"><Company>Example Fab</Company></Properties>'
        ).encode()
        chunks = [
            SimpleNamespace(
                name=name,
                is_inline=True,
                original_compressed=zlib.compress(value, wbits=-15),
                usz=len(value),
            )
            for name, value in (("docProps/core.xml", core), ("docProps/app.xml", app))
        ]
        document = SimpleNamespace(fmt="modern", chunks=chunks)
        backend = SimpleNamespace(read=lambda _path: document)
        with TemporaryDirectory() as directory:
            source = Path(directory) / "example.SLDPRT"
            source.write_bytes(bytes.fromhex("0000000000000004140006000800"))
            self.assertTrue(is_modern_solidworks(source))
            with patch("machina_deid.solidworks._load_backend", return_value=backend):
                self.assertEqual(
                    read_identity_metadata(source),
                    [
                        ("author", "solidworks.creator", "April Example"),
                        ("organization", "solidworks.company", "Example Fab"),
                    ],
                )

    def test_rewrites_identity_text_in_core_and_app_preserving_other_xml(self):
        core = (
            f'<cp:coreProperties xmlns:cp="{CORE_NS}" xmlns:dc="{DC_NS}">'
            '<dc:creator xml:lang="en">Alice &amp; Co</dc:creator>'
            "<dc:lastModifiedBy><![CDATA[Alice & Co]]></dc:lastModifiedBy>"
            "<cp:revision>Alice &amp; Co</cp:revision></cp:coreProperties>"
        ).encode()
        app = (
            f'<Properties xmlns="{EXTENDED_NS}">'
            "<Company>Alice &amp; Co</Company><Template>Alice &amp; Co</Template>"
            "</Properties>"
        ).encode()

        def replace(value: str) -> str:
            return value.replace("Alice & Co", "Q < R")

        result = _rewrite_xml_metadata(core, "docProps/core.xml", replace)
        self.assertIn(b'<dc:creator xml:lang="en">Q &lt; R</dc:creator>', result)
        self.assertIn(b"<dc:lastModifiedBy>Q &lt; R</dc:lastModifiedBy>", result)
        self.assertIn(b"<cp:revision>Alice &amp; Co</cp:revision>", result)
        self.assertEqual(
            _rewrite_xml_metadata(app, "docProps/app.xml", replace),
            app.replace(b"<Company>Alice &amp; Co", b"<Company>Q &lt; R"),
        )

    def test_global_and_config_scopes_keep_formulas_typed_values_and_keys(self):
        sections = (
            '<propertySection name="DocumentSummaryInformation">'
            '<property name="Company"><vt:lpstr>Alice</vt:lpstr></property></propertySection>'
            '<propertySection name="UserDefinedProperties">'
            '<property name="Company" TypeID="30"><vt:lpstr>Alice</vt:lpstr>'
            "<FPVals><vt:lpstr>Alice</vt:lpstr></FPVals></property>"
            '<property name="Material" TypeID="30"><vt:lpstr>Alice</vt:lpstr></property>'
            '<property name="Customer" IsEquation="True"><vt:lpstr>Alice</vt:lpstr></property>'
            '<property name="Part Number"><vt:lpstr>&quot;SW-Filename@@Default@Part.sldprt&quot;</vt:lpstr>'
            "<FPVals><vt:lpstr>Alice</vt:lpstr></FPVals></property>"
            '<property name="Contact"><vt:i4>123</vt:i4></property></propertySection>'
        )
        formats = (
            ("docProps/custom.xml", "Properties", CUSTOM_NS),
            ("docProps/Config-12-Properties.xml", "ConfigProperties", CONFIG_NS),
        )
        for stream, root, namespace in formats:
            with self.subTest(stream=stream):
                data = (
                    f'<s:{root} xmlns:s="{namespace}" xmlns:vt="{VALUE_NS}">'
                    f"{sections}</s:{root}>"
                ).encode()
                result = _rewrite_xml_metadata(
                    data, stream, lambda value: value.replace("Alice", "Name-1")
                )
                self.assertEqual(result.count(b">Name-1</vt:lpstr>"), 2)
                self.assertEqual(result.count(b">Alice</vt:lpstr>"), 4)
                self.assertIn(b'<property name="Company" TypeID="30">', result)
                self.assertIn(b'&quot;SW-Filename@@Default@Part.sldprt&quot;', result)
                self.assertIn(b"<vt:i4>123</vt:i4>", result)

    def test_no_op_unavailable_and_unverified_write_differ(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.sldprt"
            target = Path(directory) / "destination.sldprt"
            target.write_bytes(b"existing destination")
            document = SimpleNamespace(
                fmt="modern", chunks=[SimpleNamespace(name="_MO_VERSION_12000")]
            )
            backend = SimpleNamespace(
                read=lambda _path: document,
                version=lambda _names: 12000,
                tested_versions=frozenset({15000}),
            )
            with (
                patch("machina_deid.solidworks._load_backend", return_value=backend),
                patch("machina_deid.solidworks._plan_metadata_edits", return_value={}),
            ):
                self.assertFalse(rewrite_solidworks(source, target, str.upper))
            with (
                patch("machina_deid.solidworks._load_backend", return_value=backend),
                patch("machina_deid.solidworks._plan_metadata_edits", return_value={8: b"change"}),
                self.assertRaisesRegex(SolidWorksUnsupported, "no verified write support"),
            ):
                rewrite_solidworks(source, target, str.upper)
            with (
                patch("machina_deid.solidworks._load_backend", side_effect=SolidWorksUnavailable),
                self.assertRaises(SolidWorksUnavailable),
            ):
                rewrite_solidworks(source, target, str.upper)
            self.assertEqual(target.read_bytes(), b"existing destination")

    def test_verify_rejects_changes_to_non_metadata_bytes(self):
        geometry = b"geometry"
        old_payload = zlib.compress(b"old", wbits=-15)
        new_payload = zlib.compress(b"new", wbits=-15)
        old_chunk = SimpleNamespace(
            offset=8, end=8 + len(old_payload), name="docProps/core.xml"
        )
        new_chunk = SimpleNamespace(
            offset=8,
            end=8 + len(new_payload),
            name="docProps/core.xml",
            original_compressed=new_payload,
            usz=3,
        )
        original = SimpleNamespace(fmt="modern", data=geometry + old_payload, chunks=[old_chunk])
        reopened = SimpleNamespace(fmt="modern", chunks=[new_chunk])
        backend = SimpleNamespace(
            serialize=lambda _doc: b"Geometry" + new_payload,
            read_bytes=lambda _data: reopened,
            find_toc_record=lambda *_args: None,
        )

        with self.assertRaisesRegex(SolidWorksError, "outside the approved metadata"):
            _render_verified(original, {8: b"new"}, backend)


if __name__ == "__main__":
    unittest.main()
