import os
import shlex
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from machina_deid.dwg import rewrite_dwg
from machina_deid.oda import operator_login

FAKE = Path(__file__).with_name("fake_oda.py").resolve()
FAKE_COMMAND = f"{shlex.quote(sys.executable)} {shlex.quote(str(FAKE))}"


def drawing_dxf(text=b"ACME", xref=b"site.dwg"):
    return (
        b"  0\nSECTION\n  2\nHEADER\n  9\n$ACADVER\n  1\nAC1027\n  9\n$LASTSAVEDBY\n  1\nmachina\n"
        b"  0\nENDSEC\n  0\nSECTION\n  2\nBLOCKS\n  0\nBLOCK\n  5\n20\n  2\nSITE\n  1\n" + xref +
        b"\n  0\nENDBLK\n  5\n21\n  0\nENDSEC\n  0\nSECTION\n  2\nENTITIES\n"
        b"  0\nTEXT\n  5\n2A\n  8\n0\n 10\n1.0\n  1\n" + text + b"\n  0\nENDSEC\n  0\nEOF\n"
    )


def fake_dwg(text=b"ACME", version=b"AC1027", xref=b"site.dwg"):
    return version + b"\n" + drawing_dxf(text, xref)


def swap(value):
    return value.replace("ACME", "NOVA")


class DwgTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        patcher = mock.patch.dict(os.environ, {"MACHINA_ODA_CONVERTER": FAKE_COMMAND})
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_adapter(self, data):
        source, destination = self.folder / "in.dwg", self.folder / "out.dwg"
        source.write_bytes(data)
        return rewrite_dwg(source, destination, swap), destination

    def test_dwg_text_rewritten_in_same_version(self):
        changed, destination = self.run_adapter(fake_dwg())
        self.assertTrue(changed)
        output = destination.read_bytes()
        self.assertTrue(output.startswith(b"AC1027"))
        self.assertIn(b"NOVA", output)
        self.assertNotIn(b"ACME", output)

    def test_dwg_without_text_edits_is_still_rewritten_to_normalize_last_saved_by(self):
        changed, destination = self.run_adapter(fake_dwg(text=b"plain"))
        self.assertTrue(changed)
        self.assertTrue(destination.read_bytes().startswith(b"AC1027"))

    def test_dwg_with_thumbnail_round_trips(self):
        data = fake_dwg().replace(
            b"  0\nSECTION\n  2\nBLOCKS",
            b"  0\nSECTION\n  2\nTHUMBNAILIMAGE\n 90\n2\n310\nFFFF\n  0\nENDSEC\n  0\nSECTION\n  2\nBLOCKS",
        )
        changed, destination = self.run_adapter(data)
        self.assertTrue(changed)
        self.assertNotIn(b"THUMBNAILIMAGE", destination.read_bytes())

    def test_changed_geometry_is_rejected(self):
        with mock.patch.dict(os.environ, {"FAKE_ODA_MUTATE": "1"}):
            with self.assertRaisesRegex(ValueError, "changed drawing structure"):
                self.run_adapter(fake_dwg())

    def test_unknown_version_raises(self):
        with self.assertRaisesRegex(ValueError, "Unsupported DWG version"):
            self.run_adapter(fake_dwg(version=b"AC1009"))

    def test_converter_login_stamp_is_rejected(self):
        with mock.patch.dict(os.environ, {"FAKE_ODA_STAMP": operator_login()}):
            with self.assertRaisesRegex(ValueError, "local login"):
                self.run_adapter(fake_dwg())

    def test_missing_converter_raises(self):
        with mock.patch.dict(os.environ, {"MACHINA_ODA_CONVERTER": "", "PATH": ""}):
            with self.assertRaisesRegex(ValueError, "needs ODA File Converter"):
                self.run_adapter(fake_dwg())


if __name__ == "__main__":
    unittest.main()
