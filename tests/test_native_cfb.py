import importlib.util
import struct
import tempfile
import unittest
from pathlib import Path

from machina_deid.native_cfb import (
    CFB_SIGNATURE,
    DOCUMENT_SUMMARY_FMTID,
    DOCUMENT_SUMMARY_STREAM,
    SUMMARY_FMTID,
    SUMMARY_STREAM,
    USER_DEFINED_FMTID,
    NativeCfbError,
    _rewrite_property_stream,
    rewrite_native_cfb,
)

FREE_SECTOR = 0xFFFFFFFF
END_OF_CHAIN = 0xFFFFFFFE
FAT_SECTOR = 0xFFFFFFFD


def code_page(number: int) -> bytes:
    return struct.pack("<IHH", 2, number, 0)


def text_property(value: str, encoding: str, *, wide: bool = False) -> bytes:
    encoded = (value + "\x00").encode(encoding)
    return struct.pack("<II", 0x1F if wide else 0x1E, len(encoded) // (2 if wide else 1)) + encoded


def property_stream(*sections: tuple[bytes, dict[int, bytes]]) -> bytes:
    header = struct.pack("<HHI16sI", 0xFFFE, 0, 0, b"\x00" * 16, len(sections))
    descriptors, payload = bytearray(), bytearray()
    for fmtid, properties in sections:
        descriptors.extend(struct.pack("<16sI", fmtid, 28 + 20 * len(sections) + len(payload)))
        table, values = bytearray(), bytearray()
        for property_id, value in properties.items():
            table.extend(struct.pack("<II", property_id, 8 + 8 * len(properties) + len(values)))
            values.extend(value + b"\x00" * (-len(value) % 4))
        payload.extend(struct.pack("<II", 8 + len(table) + len(values), len(properties)))
        payload.extend(table + values)
    return header + descriptors + payload


def summary_stream() -> bytes:
    return property_stream(
        (
            SUMMARY_FMTID,
            {
                1: code_page(1252),
                4: text_property("Alice Engineering", "cp1252"),
                7: text_property("Alice Engineering.template", "cp1252"),
                8: text_property("Zoë Example\x00", "utf-16-le", wide=True),
                18: text_property("Alice Engineering", "cp1252"),
            },
        )
    )


def document_summary_stream() -> bytes:
    names = ((2, "Customer Name"), (3, "Material"))
    dictionary = struct.pack("<I", len(names))
    for property_id, label in names:
        encoded = (label + "\x00").encode("utf-16-le")
        dictionary += (
            struct.pack("<II", property_id, len(encoded) // 2)
            + encoded
            + b"\x00" * (-len(encoded) % 4)
        )
    return property_stream(
        (DOCUMENT_SUMMARY_FMTID, {1: code_page(1252), 15: text_property("Société Test", "cp1252")}),
        (
            USER_DEFINED_FMTID,
            {
                0: dictionary,
                1: code_page(1200),
                2: text_property("Société Test", "utf-16-le"),
                3: text_property("Société Test", "utf-16-le"),
            },
        ),
    )


def compound_fixture() -> bytes:
    streams = (summary_stream(), document_summary_stream(), b"Alice Engineering geometry\x00")
    header = bytearray(512)
    header[:8] = CFB_SIGNATURE
    struct.pack_into("<5H", header, 24, 0x3E, 3, 0xFFFE, 9, 6)
    struct.pack_into("<9I", header, 40, 0, 1, 1, 0, 4096, 2, 1, END_OF_CHAIN, 0)
    struct.pack_into("<109I", header, 76, 0, *([FREE_SECTOR] * 108))

    names = ("Root Entry", SUMMARY_STREAM, DOCUMENT_SUMMARY_STREAM, "Geometry")
    directory = bytearray(512)
    mini_stream = bytearray()
    mini_fat: list[int] = []
    for index, (name, content) in enumerate(zip(names[1:], streams, strict=True), start=1):
        first_mini_sector = len(mini_fat)
        count = (len(content) + 63) // 64
        mini_fat.extend(range(first_mini_sector + 1, first_mini_sector + count))
        mini_fat.append(END_OF_CHAIN)
        mini_stream.extend(content.ljust(count * 64, b"\x00"))
        position = index * 128
        encoded = (name + "\x00").encode("utf-16-le")
        directory[position:position + len(encoded)] = encoded
        left, right = (3, 2) if index == 1 else (FREE_SECTOR, FREE_SECTOR)
        struct.pack_into(
            "<HBBIII", directory, position + 64, len(encoded), 2, int(index == 1), left, right, FREE_SECTOR
        )
        struct.pack_into("<IQ", directory, position + 116, first_mini_sector, len(content))

    root_name = (names[0] + "\x00").encode("utf-16-le")
    directory[:len(root_name)] = root_name
    struct.pack_into("<HBBIII", directory, 64, len(root_name), 5, 1, FREE_SECTOR, FREE_SECTOR, 1)
    struct.pack_into("<IQ", directory, 116, 3, len(mini_stream))
    mini_fat_bytes = struct.pack("<128I", *mini_fat, *([FREE_SECTOR] * (128 - len(mini_fat))))
    stream_sectors = (len(mini_stream) + 511) // 512
    fat = [FAT_SECTOR, END_OF_CHAIN, END_OF_CHAIN, *range(4, 3 + stream_sectors), END_OF_CHAIN]
    fat_bytes = struct.pack("<128I", *fat, *([FREE_SECTOR] * (128 - len(fat))))
    return bytes(header) + fat_bytes + directory + mini_fat_bytes + mini_stream.ljust(
        stream_sectors * 512, b"\x00"
    )


class NativePropertyTests(unittest.TestCase):
    def test_custom_dictionary_targets_identity_values_and_retains_keys(self):
        original = document_summary_stream()
        rewritten = _rewrite_property_stream(
            original, DOCUMENT_SUMMARY_STREAM, lambda value: value.replace("Société Test", "Firma")
        )

        self.assertEqual(len(original), len(rewritten))
        self.assertIn(b"Firma\x00", rewritten)
        self.assertIn("Firma\x00".encode("utf-16-le"), rewritten)
        self.assertIn("Société Test\x00".encode("utf-16-le"), rewritten)
        self.assertIn("Customer Name\x00".encode("utf-16-le"), rewritten)

    def test_utf16_counts_code_units_and_leaves_structural_values(self):
        original = summary_stream()
        rewritten = _rewrite_property_stream(
            original,
            SUMMARY_STREAM,
            lambda value: {"Alice Engineering": "Casey", "Zoë Example": "A😀"}.get(value, value),
        )

        self.assertEqual(len(original), len(rewritten))
        self.assertIn(text_property("A😀\x00", "utf-16-le", wide=True), rewritten)
        self.assertEqual(rewritten.count(b"Alice Engineering"), 2)
        self.assertIn(b"Casey\x00", rewritten)

    def test_oversized_replacement_and_corrupt_section_offset_are_rejected(self):
        data = summary_stream()
        with self.assertRaisesRegex(NativeCfbError, "existing property space"):
            _rewrite_property_stream(data, SUMMARY_STREAM, lambda value: value * 10)
        invalid = bytearray(data)
        struct.pack_into("<I", invalid, 44, 2)
        with self.assertRaisesRegex(NativeCfbError, "section offset"):
            _rewrite_property_stream(bytes(invalid), SUMMARY_STREAM, str.upper)

    @unittest.skipUnless(importlib.util.find_spec("olefile"), "Install olefile to test native CFB writing")
    def test_updates_real_compound_mini_streams_without_editing_other_streams(self):
        import olefile

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.SLDPRT"
            target = Path(directory) / "output.SLDPRT"
            source.write_bytes(compound_fixture())

            self.assertTrue(
                rewrite_native_cfb(
                    source, target, lambda value: value.replace("Alice Engineering", "Casey")
                )
            )
            self.assertEqual(source.stat().st_size, target.stat().st_size)
            with olefile.OleFileIO(source) as original, olefile.OleFileIO(target) as edited:
                self.assertEqual(original.listdir(), edited.listdir())
                for stream in (DOCUMENT_SUMMARY_STREAM, "Geometry"):
                    self.assertEqual(original.openstream(stream).read(), edited.openstream(stream).read())
                self.assertEqual(edited.getproperties(SUMMARY_STREAM)[4], b"Casey")
                self.assertEqual(len(edited.openstream(SUMMARY_STREAM).read()), len(summary_stream()))
            self.assertEqual(source.read_bytes(), compound_fixture())

            with self.assertRaises(NativeCfbError):
                rewrite_native_cfb(source, target, lambda value: value * 100)
            with olefile.OleFileIO(target) as edited:
                self.assertEqual(edited.getproperties(SUMMARY_STREAM)[4], b"Casey")


if __name__ == "__main__":
    unittest.main()
