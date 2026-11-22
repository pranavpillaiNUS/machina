import io
import stat
import tarfile
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path

from machina_deid.archives import ArchiveError, ArchiveLimits, archive_suffix, rewrite_archive


def rewrite_text(name: str, source: Path, destination: Path) -> bool:
    if not name.endswith(".txt"):
        return False
    content = source.read_bytes()
    replacement = content.replace(b"Original Business", b"Example Company")
    if content == replacement:
        return False
    destination.write_bytes(replacement)
    return True


class ArchiveTests(unittest.TestCase):
    def test_nested_tar_zip_round_trip_preserves_order_and_payloads(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = root / "input.zip", root / "output.zip"
            nested = io.BytesIO()
            with tarfile.open(fileobj=nested, mode="w:gz") as archive:
                item = tarfile.TarInfo("./Project/title.txt")
                value = b"Original Business\n"
                item.size = len(value)
                item.mode = 0o640
                archive.addfile(item, io.BytesIO(value))
            with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.comment = b"Source comment"
                archive.writestr("members/", b"")
                archive.writestr("members/drawing.pdf", b"original geometry")
                archive.writestr("members/bundle.TAR.GZ", nested.getvalue())
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", category=UserWarning)
                    archive.writestr("members/drawing.pdf", b"second drawing")

            result = rewrite_archive(source, target, transform=rewrite_text)

            self.assertTrue(result.changed)
            self.assertEqual(result.members, 5)
            self.assertEqual(result.changed_members, 2)
            with zipfile.ZipFile(target) as archive:
                entries = archive.infolist()
                self.assertEqual(
                    [entry.filename for entry in entries],
                    ["members/", "members/drawing.pdf", "members/bundle.TAR.GZ", "members/drawing.pdf"],
                )
                self.assertEqual(archive.comment, b"Source comment")
                self.assertEqual(archive.read(entries[1]), b"original geometry")
                self.assertEqual(archive.read(entries[3]), b"second drawing")
                with tarfile.open(fileobj=io.BytesIO(archive.read(entries[2])), mode="r:gz") as tar:
                    self.assertEqual(tar.getnames(), ["./Project/title.txt"])
                    item = tar.getmember("./Project/title.txt")
                    self.assertEqual(item.mode, 0o640)
                    with tar.extractfile(item) as payload:
                        self.assertEqual(payload.read(), b"Example Company\n")

    def test_unchanged_archive_keeps_identical_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = root / "source.zip", root / "target.zip"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("geometry.STEP", b"test")

            result = rewrite_archive(source, target, transform=rewrite_text)

            self.assertFalse(result.changed)
            self.assertEqual(source.read_bytes(), target.read_bytes())
            self.assertEqual(archive_suffix("nested.Tar.Xz"), ".tar.xz")
            self.assertIsNone(archive_suffix("native.FCStd"))

    def test_zip_rejects_traversal_and_keeps_existing_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = root / "original.zip", root / "target.zip"
            target.write_bytes(b"existing result")
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("title.txt", b"Original Business")
                archive.writestr("../confidential", b"unsafe")

            with self.assertRaisesRegex(ArchiveError, "unsafe path"):
                rewrite_archive(source, target, transform=rewrite_text)

            self.assertEqual(target.read_bytes(), b"existing result")
            self.assertEqual(set(root.iterdir()), {source, target})

    def test_tar_and_zip_links_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tar_path, zip_path, target = root / "links.tar", root / "links.zip", root / "target.zip"
            with tarfile.open(tar_path, "w") as archive:
                item = tarfile.TarInfo("native_model")
                item.type = tarfile.SYMTYPE
                item.linkname = "../outside"
                archive.addfile(item)
            with zipfile.ZipFile(zip_path, "w") as archive:
                item = zipfile.ZipInfo("native_model")
                item.create_system = 3
                item.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(item, b"../outside")

            for source in (tar_path, zip_path):
                with self.subTest(suffix=source.suffix), self.assertRaisesRegex(
                    ArchiveError, "unsupported member type"
                ):
                    rewrite_archive(source, target, transform=rewrite_text)
            self.assertFalse(target.exists())

    def test_casefold_rename_collision_and_expansion_limits_abort(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = root / "source.zip", root / "target.zip"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("A/title.txt", b"long enough to trigger a small limit")
                archive.writestr("B/title.txt", b"Original Business")

            rename = {"A/title.txt": "C/title.txt", "B/title.txt": "c/title.txt"}.__getitem__
            with self.assertRaisesRegex(ArchiveError, "paths collide"):
                rewrite_archive(source, target, transform=rewrite_text, rename=rename)
            with self.assertRaisesRegex(ArchiveError, "expansion"):
                rewrite_archive(
                    source, target, transform=rewrite_text, limits=ArchiveLimits(max_member_bytes=32)
                )
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
