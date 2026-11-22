import os
import shlex
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from machina_deid.oda import convert, converter_command, dwg_version

FAKE = Path(__file__).with_name("fake_oda.py").resolve()
FAKE_COMMAND = f"{shlex.quote(sys.executable)} {shlex.quote(str(FAKE))}"
DXF = b"  0\nEOF\n"


def fake_environment(**extra):
    return mock.patch.dict(os.environ, {"MACHINA_ODA_CONVERTER": FAKE_COMMAND, **extra})


class OdaTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def drawing(self, header=b"AC1032"):
        path = self.folder / "Odd name (ACME).dwg"
        path.write_bytes(header + b"\n" + DXF)
        return path

    def test_dwg_version_known_and_rejected(self):
        self.assertEqual(dwg_version(self.drawing()), "AC1032")
        for header in (b"AC1009", b"MC0.0"):
            with self.assertRaisesRegex(ValueError, "Unsupported DWG version"):
                dwg_version(self.drawing(header))

    def test_command_uses_env_override(self):
        with fake_environment():
            self.assertEqual(converter_command(), [sys.executable, str(FAKE)])

    def stub_on_path(self):
        bin_dir = self.folder / "bin"
        bin_dir.mkdir()
        stub = bin_dir / "ODAFileConverter"
        stub.write_text("#!/bin/sh\nprintf '%s|%s' \"$DISPLAY\" \"$WAYLAND_DISPLAY\" > \"$2/drawing.dxf\"\n")
        stub.chmod(0o755)
        return str(bin_dir)

    def test_path_binary_with_only_a_visible_display_is_refused(self):
        environment = {"MACHINA_ODA_CONVERTER": "", "MACHINA_ODA_DISPLAY": "", "PATH": self.stub_on_path(), "DISPLAY": ":0"}
        with mock.patch.dict(os.environ, environment):
            self.assertIsNone(converter_command())
            with self.assertRaisesRegex(ValueError, "hidden display"):
                convert(self.drawing(), self.folder, "ACAD2018", "DXF")

    def test_hidden_display_reaches_the_converter(self):
        environment = {
            "MACHINA_ODA_CONVERTER": "", "MACHINA_ODA_DISPLAY": ":97", "PATH": self.stub_on_path(),
            "DISPLAY": ":0", "WAYLAND_DISPLAY": "wayland-0",
        }
        with mock.patch.dict(os.environ, environment):
            output = convert(self.drawing(), self.folder, "ACAD2018", "DXF")
        self.assertEqual(output.read_text(), ":97|")

    def test_convert_round_trip_with_fake(self):
        out = self.folder / "out"
        out.mkdir()
        with fake_environment():
            dxf = convert(self.drawing(), out, "ACAD2018", "DXF")
            self.assertEqual(dxf.read_bytes(), DXF)
            dwg = convert(dxf, out, "ACAD2018", "DWG")
        self.assertEqual(dwg.suffix, ".dwg")
        self.assertEqual(dwg_version(dwg), "AC1032")

    def test_convert_empty_output_raises(self):
        with fake_environment(FAKE_ODA_MODE="empty"):
            with self.assertRaisesRegex(ValueError, "no output"):
                convert(self.drawing(), self.folder, "ACAD2018", "DXF")

    def test_convert_timeout_raises(self):
        with fake_environment(FAKE_ODA_MODE="hang"):
            with self.assertRaisesRegex(ValueError, "timed out"):
                convert(self.drawing(), self.folder, "ACAD2018", "DXF", timeout=1)


if __name__ == "__main__":
    unittest.main()
