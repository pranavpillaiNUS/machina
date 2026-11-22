"""Enumerate regular engineering files without flattening corpus paths."""

from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import re
import stat


@dataclass(frozen=True, slots=True)
class CorpusFile:
    relative: PurePosixPath
    size: int


_LFS_VERSION = {
    b"version https://git-lfs.github.com/spec/v1",
    b"version https://hawser.github.com/spec/v1",
}
_LFS_LINE = re.compile(rb"([a-z0-9.-]+) ([^\r\n]+)")
_LFS_OID = re.compile(rb"sha256:[a-f0-9]{64}")


def is_lfs_pointer(path: Path, size: int) -> bool:
    """Recognize a pointer to unavailable Git LFS content without fetching it."""
    if not 110 <= size < 1024:
        return False
    with path.open("rb") as source:
        raw = source.read(1024)
    lines = raw.split(b"\n")
    if lines[0] not in _LFS_VERSION or lines[-1] != b"":
        return False
    fields = {}
    for line in lines[1:-1]:
        match = _LFS_LINE.fullmatch(line)
        if match is None or match[1] in fields:
            return False
        fields[match[1]] = match[2]
    return bool(
        b"version" not in fields
        and _LFS_OID.fullmatch(fields.get(b"oid", b""))
        and fields.get(b"size", b"").isdigit()
    )


def filesystem_path(path: Path) -> Path:
    """Use Windows extended-length spelling for an absolute filesystem path."""
    if os.name != "nt":
        return path
    value = str(path.absolute())
    if value.startswith("\\\\?\\"):
        return Path(value)
    if value.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + value[2:])
    return Path("\\\\?\\" + value)


def scan(root: Path) -> list[CorpusFile]:
    """Find source files in deterministic order and exclude downloader state."""
    if not root.is_dir():
        raise ValueError("Input must be a directory.")

    def raise_walk_error(error: OSError) -> None:
        raise error

    result = []
    for directory, dirs, files in os.walk(root, onerror=raise_walk_error):
        current = Path(directory)
        if current == root and ".get-input" in dirs:
            dirs.remove(".get-input")
        dirs.sort()
        file_names = set(files)
        for name in sorted(dirs + files):
            path = current / name
            metadata = path.lstat()
            attributes = getattr(metadata, "st_file_attributes", 0)
            if stat.S_ISLNK(metadata.st_mode) or attributes & getattr(
                stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0
            ):
                raise ValueError("Input contains a symbolic link or reparse point.")
            if name in file_names:
                if not stat.S_ISREG(metadata.st_mode):
                    raise ValueError("Input contains a nonregular file.")
                result.append(
                    CorpusFile(PurePosixPath(*path.relative_to(root).parts), metadata.st_size)
                )
    return result
