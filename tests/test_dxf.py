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


    def test_header_identity_and_custom_properties_rewrite_in_place(self):
        data = (
            b"  0\nSECTION\n  2\nHEADER\n  9\n$LASTSAVEDBY\n  1\nACME\n"
            b"  9\n$CUSTOMPROPERTYTAG\n  1\nClient\n  9\n$CUSTOMPROPERTY\n  1\nACME Ltd\n"
            b"  9\n$PROJECTNAME\n  1\nACME Tower\n  9\n$HANDSEED\n  5\nACME\n  0\nENDSEC\n  0\nEOF\n"
        )
        out = rewrite_dxf_bytes(data, swap)
        self.assertEqual(out, data.replace(b"1\nACME", b"1\nNOVA"))
        self.assertIn(b"$HANDSEED\n  5\nACME", out)

    def test_xrecord_field_and_table_strings(self):
        data = (
            b"  0\nXRECORD\n  5\n1F\n  1\nACME\n300\nACME\n"
            b"  0\nFIELD\n  2\n%<\\AcVar Author>%\n  1\nACME\n"
            b"  0\nACAD_TABLE\n  2\n*T1\n  1\n{\\fArial;ACME}\n  8\nACME\n  0\nEOF\n"
        )
        out = rewrite_dxf_bytes(data, swap)
        self.assertEqual(out.count(b"NOVA"), 4)
        self.assertIn(b"  5\n1F", out)
        self.assertIn(b"  8\nACME", out)


if __name__ == "__main__":
    unittest.main()
