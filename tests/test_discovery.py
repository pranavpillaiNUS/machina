import tempfile
import unittest
from pathlib import Path

from machina_deid.discovery import InventoryError, inventory_local, inventory_manifest


class DiscoveryTests(unittest.TestCase):
    def test_manifest_counts_conflicts_and_hides_unrecognized_suffixes(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "manifest.tsv"
            manifest.write_text(
                "# header\n"
                "FirstProject/A.STEP\t14\t\tmirror/a\n"
                "firstproject/B.pdf\t12\t\tmirror/b\n"
                "FirstProject/a.step\t10\t\tmirror/c\n"
                "FirstProject/A.STEP\t14\t\tmirror/a\n"
                "archive.tar.gz\t2\t\tmirror/d\n"
                "part.PrivateIdentity\t3\t\tmirror/e\n"
                "empty\t0\t\t\n",
                encoding="utf-8",
            )

            summary = inventory_manifest(manifest).as_dict()

        self.assertEqual(summary["files"], 6)
        self.assertEqual(summary["bytes"], 41)
        self.assertEqual(summary["empty_files"], 1)
        self.assertEqual(summary["duplicate_path_rows"], 1)
        self.assertEqual(summary["casefold_collision_groups"], 2)
        self.assertEqual(summary["casefold_collision_files"], 3)
        self.assertEqual(summary["by_suffix"][".step"], {"files": 2, "bytes": 24})
        self.assertEqual(summary["by_suffix"][".tar.gz"], {"files": 1, "bytes": 2})
        self.assertEqual(summary["by_suffix"]["(other)"], {"files": 1, "bytes": 3})
        self.assertNotIn("PrivateIdentity", str(summary))
        self.assertNotIn("FirstProject", str(summary))

    def test_manifest_validation_uses_row_numbers_only(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "manifest.tsv"
            manifest.write_text("# header\n../private.pdf\t2\t\tmirror/a\n", encoding="utf-8")

            with self.assertRaisesRegex(InventoryError, "^Manifest line 2 needs a relative path"):
                inventory_manifest(manifest)

    def test_local_scan_skips_only_top_level_downloader_scratch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scratch = root / ".get-input"
            scratch.mkdir()
            (scratch / "manifest.tsv").write_bytes(b"temporary")
            nested = root / "drawings" / ".get-input"
            nested.mkdir(parents=True)
            (nested / "original.DXF").write_bytes(b"DXF")
            (root / "drawing.PDF").write_bytes(b"PDF")

            summary = inventory_local(root).as_dict()

        self.assertEqual(summary["files"], 2)
        self.assertEqual(summary["bytes"], 6)
        self.assertEqual(
            summary["by_suffix"],
            {".dxf": {"files": 1, "bytes": 3}, ".pdf": {"files": 1, "bytes": 3}},
        )


if __name__ == "__main__":
    unittest.main()
