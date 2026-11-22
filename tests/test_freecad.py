import io
import tempfile
import unittest
import zipfile
from pathlib import Path

from machina_deid.freecad import rewrite_freecad


class FreeCADTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.source = Path(temporary.name, "input.FCStd")
        self.destination = Path(temporary.name, "output.FCStd")

    def package(self, members):
        with zipfile.ZipFile(self.source, "w") as archive:
            archive.comment = b"FreeCAD archive comment"
            for name, content in members.items():
                item = zipfile.ZipInfo(name, date_time=(2022, 2, 2, 2, 2, 2))
                item.comment = b"member comment"
                item.extra = b"\xfe\xca\x03\x00abc"
                item.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(item, content)

    @staticmethod
    def replace(value):
        return value.replace("ACME", "NOVA")

    def test_typed_fields_change_without_altering_geometry_links_and_attachment_data(self):
        document = b'''<?xml version='1.0' encoding='utf-8'?>
<Document SchemaVersion="4">
 <Properties>
  <Property name="Company" type="App::PropertyString"><String value="ACME"/></Property>
  <Property name="Label" type="App::PropertyString"><String value="ACME"/></Property>
  <Property name="UnitSystem" type="App::PropertyEnumeration"><Integer value="2"/></Property>
  <Property name="Meta" type="App::PropertyMap"><Map count="1"><Item key="ACME" value="ACME"/></Map></Property>
 </Properties>
 <Objects><Object name="ACME" type="Part::Feature"/><Object name="Template" type="TechDraw::DrawSVGTemplate"/></Objects>
 <ObjectData>
  <Object name="ACME"><Properties>
   <Property name="Label" type="App::PropertyString"><String value="ACME"/></Property>
   <Property name="Label2" type="App::PropertyString"><String value="ACME"/></Property>
   <Property name="ExpressionEngine" type="App::PropertyExpressionEngine"><ExpressionEngine count="1">
       <Expression path="Length" expression="&lt;&lt;ACME&gt;&gt;.Height"/>
   </ExpressionEngine></Property>
   <Property name="Shape" type="Part::PropertyPartShape"><Part file="ACME.brp"/></Property>
   <Property name="LinkedObject" type="App::PropertyXLinkSub"><XLink file="../ACME.FCStd" name="ACME" sub="Face3"/></Property>
   <Property name="Attached" type="App::PropertyFileIncluded"><FileIncluded file="attached.svg"/></Property>
  </Properties></Object>
  <Object name="Template"><Properties>
   <Property name="PageResult" type="App::PropertyFileIncluded"><FileIncluded file="templates/page.svg"/></Property>
   <Property name="EditableTexts" type="App::PropertyMap"><Map count="1"><Item key="ACME" value="ACME"/></Map></Property>
  </Properties></Object>
 </ObjectData>
</Document>'''
        svg = b'<svg><text x="10" y="20">ACME</text><path d="M10 10 L15 15"/></svg>'
        nested = io.BytesIO()
        with zipfile.ZipFile(nested, "w") as archive:
            archive.writestr("Document.xml", document)
        members = {
            "Document.xml": document,
            "GuiDocument.xml": b'<Document><ViewProviderData/><Camera settings="ACME"/></Document>',
            "ACME.brp": b"opaque shape\x00ACME",
            "templates/page.svg": svg,
            "attached.svg": svg,
            "nested.zip": nested.getvalue(),
        }
        self.package(members)
        self.assertTrue(rewrite_freecad(self.source, self.destination, self.replace))
        expected = (
            document.replace(b'name="Company" type="App::PropertyString"><String value="ACME"',
                             b'name="Company" type="App::PropertyString"><String value="NOVA"')
            .replace(b'name="Label2" type="App::PropertyString"><String value="ACME"',
                     b'name="Label2" type="App::PropertyString"><String value="NOVA"')
            .replace(b'<Item key="ACME" value="ACME"', b'<Item key="ACME" value="NOVA"')
            .replace(b'file="../ACME.FCStd"', b'file="../NOVA.FCStd"')
        )
        with zipfile.ZipFile(self.source) as source, zipfile.ZipFile(self.destination) as output:
            self.assertIsNone(output.testzip())
            self.assertEqual(output.namelist(), source.namelist())
            self.assertEqual(output.comment, source.comment)
            self.assertEqual(output.read("Document.xml"), expected)
            self.assertEqual(output.read("templates/page.svg"), svg.replace(b">ACME<", b">NOVA<"))
            for name in members.keys() - {"Document.xml", "templates/page.svg"}:
                self.assertEqual(output.read(name), members[name])
            for original, written in zip(source.infolist(), output.infolist()):
                self.assertEqual(written.date_time, original.date_time)
                self.assertEqual(written.external_attr, original.external_attr)
                self.assertEqual(written.extra, original.extra)
                self.assertEqual(written.comment, original.comment)

    def test_unchanged_and_unsafe_packages_leave_destination_untouched(self):
        self.package({"Document.xml": b"<Document><Properties/></Document>"})
        self.assertFalse(rewrite_freecad(self.source, self.destination, self.replace))
        self.assertFalse(self.destination.exists())

        self.package({"Document.xml": b"<Document/>", "../unsafe.svg": b"<svg/>"})
        with self.assertRaisesRegex(ValueError, "unsupported member path"):
            rewrite_freecad(self.source, self.destination, self.replace)
        self.assertFalse(self.destination.exists())

    def test_malformed_or_hostile_metadata_is_rejected(self):
        self.package({"Document.xml": b'<!DOCTYPE Document SYSTEM "https://example.invalid/unused"><Document/>'})
        with self.assertRaisesRegex(ValueError, "unsupported XML declaration"):
            rewrite_freecad(self.source, self.destination, self.replace)
        self.assertFalse(self.destination.exists())


if __name__ == "__main__":
    unittest.main()
