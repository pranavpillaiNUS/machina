"""Aggregate local and mirror inventories without exposing source paths."""

from __future__ import annotations

import os
import re
import stat
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

KNOWN_SUFFIXES = frozenset(
    """
    .3dm .3ds .3mf .7z .ai .asc .asm .bmp .bz2 .catdrawing .catpart
    .catproduct .csv .dae .doc .docx .dwf .dwfx .dwg .dxf .easm .eprt
    .eps .fcstd .fem .f3d .f3z .gif .glb .gltf .gz .iam .idw .ifc
    .ige .iges .igs .ini .ipj .ipn .ipt .jpeg .jpg .jt .json .md .nc
    .neu .obj .odg .ods .odt .p21 .parasolid .pdf .ply .png .ppt .pptx
    .prt .ps .psd .rar .rtf .sab .sat .scad .skp .sldasm .slddrw
    .sldprt .smt .step .stl .stp .svg .svgz .tar .tbz .tbz2 .tgz .tif
    .tiff .tsv .txt .txz .webp .wrl .x_b .x_t .x3d .xls .xlsm .xlsx
    .xml .xz .yaml .yml .zip .zst
    """.split()
)
COMPOUND_SUFFIXES = (".tar.bz2", ".tar.gz", ".tar.xz", ".tar.zst")
MD5 = re.compile(r"[0-9a-fA-F]{32}")


class InventoryError(ValueError):
    """An inventory could not be read without exposing source filenames."""


@dataclass(frozen=True)
class FormatTotals:
    files: int
    bytes: int


@dataclass(frozen=True)
class Inventory:
    files: int
    bytes: int
    empty_files: int
    duplicate_path_rows: int
    casefold_collision_groups: int
    casefold_collision_files: int
    skipped_non_regular: int
    by_suffix: dict[str, FormatTotals]

    def as_dict(self) -> dict[str, object]:
        return {
            "files": self.files,
            "bytes": self.bytes,
            "empty_files": self.empty_files,
            "duplicate_path_rows": self.duplicate_path_rows,
            "casefold_collision_groups": self.casefold_collision_groups,
            "casefold_collision_files": self.casefold_collision_files,
            "skipped_non_regular": self.skipped_non_regular,
            "by_suffix": {
                suffix: {"files": totals.files, "bytes": totals.bytes}
                for suffix, totals in sorted(self.by_suffix.items())
            },
        }


def inventory_local(root: Path) -> Inventory:
    """Read regular file sizes and skip the mirror's top-level scratch directory."""
    try:
        if root.is_symlink() or not root.is_dir():
            raise InventoryError("The inventory root must be a directory.")
        return _summarize(_local_records(root))
    except OSError:
        raise InventoryError("Reading the local inventory failed.") from None


def inventory_manifest(path: Path) -> Inventory:
    """Read the downloader's four-column, UTF-8 mirror manifest."""
    try:
        with path.open(encoding="utf-8-sig") as manifest:
            return _summarize(_manifest_records(manifest))
    except (OSError, UnicodeError):
        raise InventoryError("Reading the UTF-8 manifest failed.") from None


def _local_records(root: Path) -> Iterator[tuple[str, int | None]]:
    pending = [root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as children:
            for entry in children:
                if directory == root and entry.name == ".get-input":
                    continue
                path = Path(entry.path)
                properties = entry.stat(follow_symlinks=False)
                is_junction = (
                    getattr(properties, "st_reparse_tag", 0)
                    == getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003)
                )
                if stat.S_ISDIR(properties.st_mode) and not is_junction:
                    pending.append(path)
                else:
                    size = properties.st_size if stat.S_ISREG(properties.st_mode) else None
                    yield path.relative_to(root).as_posix(), size


def _manifest_records(lines: Iterable[str]) -> Iterator[tuple[str, int]]:
    for line_number, line in enumerate(lines, start=1):
        line = line.rstrip("\r\n")
        if not line or line.startswith("#"):
            continue
        columns = line.split("\t")
        if len(columns) != 4:
            raise InventoryError(f"Manifest line {line_number} must contain four columns.")
        path, byte_count, digest, url = columns
        if "\x00" in path or any(part in {"", ".", ".."} for part in path.split("/")):
            raise InventoryError(f"Manifest line {line_number} needs a relative path.")
        if not byte_count.isascii() or not byte_count.isdecimal() or len(byte_count) > 20:
            raise InventoryError(f"Manifest line {line_number} needs a valid byte count.")
        size = int(byte_count)
        if digest and MD5.fullmatch(digest) is None:
            raise InventoryError(f"Manifest line {line_number} contains an invalid MD5.")
        if size and not url:
            raise InventoryError(f"Manifest line {line_number} needs a URL path.")
        yield path, size


def _suffix(name: str) -> str:
    lower_name = name.lower()
    for suffix in COMPOUND_SUFFIXES:
        if lower_name.endswith(suffix):
            return suffix
    suffix = PurePosixPath(lower_name).suffix
    if not suffix:
        return "(none)"
    if suffix in KNOWN_SUFFIXES:
        return suffix
    # A suffix outside the known set could itself be a private name or identifier.
    return "(other)"


def _summarize(records: Iterable[tuple[str, int | None]]) -> Inventory:
    paths: set[str] = set()
    spelling: dict[str, tuple[str, bool]] = {}
    collisions: set[str] = set()
    counts: Counter[str] = Counter()
    totals: Counter[str] = Counter()
    empty_files = duplicate_path_rows = skipped_non_regular = 0

    for path, size in records:
        if size is None:
            skipped_non_regular += 1
            continue
        if path in paths:
            duplicate_path_rows += 1
            continue
        paths.add(path)
        suffix = _suffix(path.rsplit("/", 1)[-1])
        counts[suffix] += 1
        totals[suffix] += size
        empty_files += size == 0
        prefix = path
        is_file = True
        while prefix:
            folded = prefix.casefold()
            entry = prefix, is_file
            if spelling.setdefault(folded, entry) != entry:
                collisions.add(folded)
            prefix = prefix.rpartition("/")[0]
            is_file = False

    affected_files = 0
    if collisions:
        for path in paths:
            prefix = path.casefold()
            while prefix:
                if prefix in collisions:
                    affected_files += 1
                    break
                prefix = prefix.rpartition("/")[0]

    return Inventory(
        files=len(paths),
        bytes=sum(totals.values()),
        empty_files=empty_files,
        duplicate_path_rows=duplicate_path_rows,
        casefold_collision_groups=len(collisions),
        casefold_collision_files=affected_files,
        skipped_non_regular=skipped_non_regular,
        by_suffix={suffix: FormatTotals(counts[suffix], totals[suffix]) for suffix in counts},
    )
