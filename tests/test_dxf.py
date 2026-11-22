import unittest

from machina_deid.dxf import dxf_encoding, header_values, read_records, rewrite_dxf_bytes


def swap(value):
    return value.replace("ACME", "NOVA")


class DxfTests(unittest.TestCase):
    def test_rewrites_labels_and_xrefs_without_touching_handles(self):
        data = (
            b"  0\r\nTEXT\r\n  1\r\nACME\r\n  5\r\nACME\r\n 10\r\n25.00\r\n"
            b"  0\r\nBLOCK\r\n  1\r\nC:\\Drawings\\ACME.dwg\r\n"
            b"  0\r\nMTEXT\r\n  1\r\n{\\fACME;ACME}\\P3.50 mm\r\n  0\r\nEOF\r\n"
        )
        self.assertEqual(rewrite_dxf_bytes(data, swap), (
            data.replace(b"ACME\r\n  5", b"NOVA\r\n  5")
            .replace(b"ACME.dwg", b"NOVA.dwg").replace(b";ACME}", b";NOVA}")
        ))

    def test_read_records_and_header_values(self):
        data = (
            b"  0\nSECTION\n  2\nHEADER\n  9\n$ACADVER\n  1\nAC1032\n"
            b"  9\n$AUTHOR\n  1\nAna\n  0\nENDSEC\n  0\nEOF\n"
        )
        records = read_records(data)
        self.assertEqual(records[0], (0, b"SECTION"))
        self.assertEqual(header_values(records), [("$ACADVER", b"AC1032"), ("$AUTHOR", b"Ana")])
        self.assertEqual(dxf_encoding(records), "utf-8")


if __name__ == "__main__":
    unittest.main()
