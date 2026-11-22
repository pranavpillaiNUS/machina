"""Enumerate regular engineering files without flattening corpus paths."""

from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import stat


@dataclass(frozen=True, slots=True)
class CorpusFile:
    relative: PurePosixPath
    size: int


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
