"""Rewrite regular ZIP and TAR members without extracting archive paths."""

from __future__ import annotations

import copy
import gzip
import lzma
import os
import shutil
import stat
import tarfile
import tempfile
import warnings
import zipfile
import zlib
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO

ARCHIVE_SUFFIXES = {
    ".tar.bz2": "bz2",
    ".tar.gz": "gz",
    ".tar.xz": "xz",
    ".tar": "",
    ".tbz2": "bz2",
    ".tbz": "bz2",
    ".tgz": "gz",
    ".txz": "xz",
    ".zip": "zip",
}
WINDOWS_DEVICES = frozenset(
    ["CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"]
    + [f"{kind}{number}" for kind in ("COM", "LPT") for number in "123456789¹²³"]
)
COPY_CHUNK_BYTES = 1024 * 1024

MemberTransform = Callable[[str, Path, Path], bool]
MemberTransformFactory = Callable[[tuple[str, ...]], MemberTransform]
MemberRename = Callable[[str], str]


class ArchiveError(ValueError):
    """An archive could not be rewritten safely."""


@dataclass(frozen=True)
class ArchiveLimits:
    max_depth: int = 4
    max_members: int = 100_000
    max_member_bytes: int = 2_000_000_000
    max_total_bytes: int = 8_000_000_000

    def __post_init__(self) -> None:
        if min(self.max_depth, self.max_members, self.max_member_bytes, self.max_total_bytes) < 1:
            raise ArchiveError("Archive limits must be positive.")


@dataclass(frozen=True)
class ArchiveResult:
    members: int
    changed_members: int
    changed: bool


def archive_suffix(name: str) -> str | None:
    """Return a supported archive suffix with compound suffixes intact."""
    lowered = name.lower()
    return next((suffix for suffix in ARCHIVE_SUFFIXES if lowered.endswith(suffix)), None)


def rewrite_archive(
    source: Path,
    destination: Path,
    *,
    archive_name: str | None = None,
    rename: MemberRename | None = None,
    transform: MemberTransform | None = None,
    make_transform: MemberTransformFactory | None = None,
    limits: ArchiveLimits = ArchiveLimits(),
) -> ArchiveResult:
    """Rewrite one archive atomically, including supported nested archives.

    Callbacks receive member names relative to their own archive. The transform
    writes to its supplied file and returns True, or returns False to preserve the
    member bytes. A factory can scope references to each archive's own members.
    Renames preserve the member tree and must be one-to-one.
    """
    name = source.name if archive_name is None else archive_name
    if archive_suffix(name) is None:
        raise ArchiveError("Unsupported archive type.")
    if (transform is None) == (make_transform is None):
        raise ArchiveError("Supply one archive member transform or factory.")
    state = _ArchiveState(destination.parent, transform, make_transform, rename, limits)

    try:
        if source.resolve() == destination.resolve():
            raise ArchiveError("The archive source and destination must differ.")
        with _scratch_file(destination.parent) as staged:
            changed = state.rewrite(source, staged, name, depth=1)
            if not changed:
                shutil.copyfile(source, staged)
            os.replace(staged, destination)
    except (
        EOFError,
        lzma.LZMAError,
        NotImplementedError,
        OSError,
        RuntimeError,
        UnicodeError,
        tarfile.TarError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
        zlib.error,
    ):
        raise ArchiveError("The archive could not be read or written.") from None

    return ArchiveResult(state.members, state.changed_members, changed)


@contextmanager
def _scratch_file(directory: Path, suffix: str = "") -> Iterator[Path]:
    descriptor, filename = tempfile.mkstemp(prefix=".machina-archive-", suffix=suffix, dir=directory)
    path = Path(filename)
    try:
        os.close(descriptor)
        yield path
    finally:
        path.unlink(missing_ok=True)


def _member_parts(name: str, directory: bool) -> tuple[str, ...]:
    if not name or len(name) > 4096 or any(character in name for character in '\\:<>|?*"'):
        raise ArchiveError("Archive member has an unsafe path.")
    if any(ord(character) < 32 for character in name):
        raise ArchiveError("Archive member has an unsafe path.")
    if name.startswith("/"):
        raise ArchiveError("Archive member has an unsafe path.")
    while name.startswith("./"):
        name = name[2:]
    if directory:
        name = name.removesuffix("/")
        if name in {"", "."}:
            return ()
    parts = tuple(name.split("/"))
    if len(parts) > 128:
        raise ArchiveError("Archive member path has too many components.")
    for part in parts:
        if not part or part.endswith((" ", ".")):
            raise ArchiveError("Archive member has an unsafe path.")
        if part.partition(".")[0].rstrip().upper() in WINDOWS_DEVICES:
            raise ArchiveError("Archive member has an unsafe path.")
    return parts


class _ArchiveNames:
    def __init__(self, rename: MemberRename | None):
        self.rename = rename
        self.source_paths: dict[tuple[str, ...], tuple[tuple[str, ...], bool]] = {}
        self.destinations: dict[tuple[str, ...], tuple[str, ...]] = {}
        self.folded_destinations: dict[tuple[str, ...], tuple[str, ...]] = {}

    def plan(self, name: str, directory: bool) -> str:
        original = _member_parts(name, directory)
        try:
            replacement = name if self.rename is None else self.rename(name)
        except Exception:
            raise ArchiveError("Archive member rename failed.") from None
        if not isinstance(replacement, str):
            raise ArchiveError("Archive member rename must return a path.")
        new = _member_parts(replacement, directory)
        if len(original) != len(new):
            raise ArchiveError("Archive member rename changes the directory hierarchy.")
        for index in range(1, len(original) + 1):
            input_prefix, output_prefix = original[:index], new[:index]
            is_directory = directory or index < len(original)
            expected = output_prefix, is_directory
            folded_input = tuple(part.casefold() for part in input_prefix)
            folded_output = tuple(part.casefold() for part in output_prefix)
            if (
                self.source_paths.setdefault(input_prefix, expected) != expected
                or self.destinations.setdefault(output_prefix, input_prefix) != input_prefix
                or self.folded_destinations.setdefault(folded_output, folded_input) != folded_input
            ):
                raise ArchiveError("Archive member paths collide.")
        return replacement


class _ArchiveState:
    def __init__(
        self,
        scratch: Path,
        transform: MemberTransform | None,
        make_transform: MemberTransformFactory | None,
        rename: MemberRename | None,
        limits: ArchiveLimits,
    ):
        self.scratch = scratch
        self.transform = transform
        self.make_transform = make_transform
        self.rename = rename
        self.limits = limits
        self.members = 0
        self.changed_members = 0
        self.input_bytes = 0
        self.output_bytes = 0

    def rewrite(self, source: Path, target: Path, name: str, depth: int) -> bool:
        if depth > self.limits.max_depth:
            raise ArchiveError("Archive nesting exceeds its configured limit.")
        suffix = archive_suffix(name)
        if suffix is None:
            raise ArchiveError("Unsupported archive type.")
        compression = ARCHIVE_SUFFIXES[suffix]
        names = _ArchiveNames(self.rename)
        if compression == "zip":
            return self.rewrite_zip(source, target, names, depth)
        return self.rewrite_tar(source, target, compression, names, depth)

    def count_member(self, size: int) -> None:
        self.members += 1
        self.input_bytes += size
        if (
            self.members > self.limits.max_members
            or size < 0
            or size > self.limits.max_member_bytes
            or self.input_bytes > self.limits.max_total_bytes
        ):
            raise ArchiveError("Archive expansion exceeds its configured limit.")

    def scoped_transform(self, entries: Iterable[tuple[str, bool]]) -> MemberTransform:
        if self.transform is not None:
            return self.transform
        assert self.make_transform is not None
        paths = tuple("/".join(_member_parts(name, False)) for name, is_dir in entries if not is_dir)
        return self.make_transform(paths)

    def indexed_tar(self, archive: tarfile.TarFile) -> list[tarfile.TarInfo]:
        records = []
        declared = self.input_bytes
        for item in archive:
            self.check_tar_member(item)
            records.append(item)
            declared += item.size
            if (
                self.members + len(records) > self.limits.max_members
                or item.size < 0
                or item.size > self.limits.max_member_bytes
                or declared > self.limits.max_total_bytes
            ):
                raise ArchiveError("Archive expansion exceeds its configured limit.")
        return records

    @staticmethod
    def check_tar_member(item: tarfile.TarInfo) -> None:
        if (
            not (item.isdir() or item.isreg())
            or item.issparse()
            or (item.isdir() and item.size)
        ):
            raise ArchiveError("TAR contains an unsupported member type.")
        if item.linkname or "linkpath" in item.pax_headers:
            raise ArchiveError("TAR contains an unsupported link target.")

    @contextmanager
    def member_payload(
        self, stream: BinaryIO, name: str, size: int, depth: int, transform: MemberTransform
    ) -> Iterator[tuple[Path, int, bool]]:
        nested_suffix = archive_suffix(name)
        suffix = nested_suffix or PurePosixPath(name).suffix
        if len(suffix) > 24:
            suffix = ""
        with _scratch_file(self.scratch, suffix) as original:
            with original.open("wb") as staged:
                remaining = size
                while remaining:
                    chunk = stream.read(min(remaining, COPY_CHUNK_BYTES))
                    if not chunk:
                        raise ArchiveError("Archive member is shorter than declared.")
                    staged.write(chunk)
                    remaining -= len(chunk)
                if stream.read(1):
                    raise ArchiveError("Archive member is larger than declared.")

            with _scratch_file(self.scratch, suffix) as replacement:
                if nested_suffix:
                    changed = self.rewrite(original, replacement, name, depth + 1)
                else:
                    try:
                        replacement.unlink()
                        changed = transform(name, original, replacement)
                    except Exception:
                        raise ArchiveError("Archive member transform failed.") from None
                    if not isinstance(changed, bool):
                        raise ArchiveError("Archive member transform must return a Boolean.")
                path = replacement if changed else original
                if path.is_symlink() or not path.is_file():
                    raise ArchiveError("Archive member transform did not produce a regular file.")
                final_size = path.stat().st_size
                self.output_bytes += final_size
                if (
                    final_size > self.limits.max_member_bytes
                    or self.output_bytes > self.limits.max_total_bytes
                ):
                    raise ArchiveError("Archive output exceeds its configured limit.")
                yield path, final_size, changed

    def rewrite_zip(self, source: Path, target: Path, names: _ArchiveNames, depth: int) -> bool:
        changed = False
        with (
            zipfile.ZipFile(source) as original,
            zipfile.ZipFile(target, "w") as output,
            warnings.catch_warnings(),
        ):
            warnings.filterwarnings("ignore", message="Duplicate name:.*", category=UserWarning)
            output.comment = original.comment
            entries = original.infolist()
            if self.members + len(entries) > self.limits.max_members:
                raise ArchiveError("Archive expansion exceeds its configured limit.")
            transform = self.scoped_transform((item.orig_filename, item.is_dir()) for item in entries)
            for item in entries:
                is_directory = item.is_dir()
                unix_type = stat.S_IFMT(item.external_attr >> 16) if item.create_system == 3 else 0
                if (
                    unix_type not in {0, stat.S_IFDIR if is_directory else stat.S_IFREG}
                    or item.flag_bits & 1
                    or (is_directory and item.file_size)
                ):
                    raise ArchiveError("ZIP contains an unsupported member type.")
                name = names.plan(item.orig_filename, is_directory)
                if name != item.filename and is_directory and not name.endswith("/"):
                    name += "/"
                self.count_member(item.file_size)
                rewritten = copy.copy(item)
                rewritten.filename = name
                rewritten.orig_filename = name
                rewritten.extra = _zip_extra(item.extra, name != item.filename)
                with original.open(item) as stream:
                    if is_directory:
                        # Reading also verifies that the directory has no hidden payload.
                        if stream.read(1):
                            raise ArchiveError("ZIP directory contains a payload.")
                        output.writestr(rewritten, b"")
                        member_changed = name != item.filename
                    else:
                        with self.member_payload(
                            stream, item.filename, item.file_size, depth, transform
                        ) as payload:
                            path, size, payload_changed = payload
                            rewritten.file_size = size
                            with output.open(rewritten, "w") as writer:
                                with path.open("rb") as reader:
                                    shutil.copyfileobj(reader, writer, length=COPY_CHUNK_BYTES)
                        member_changed = payload_changed or name != item.filename
                self.changed_members += member_changed
                changed |= member_changed
        return changed

    def rewrite_tar(
        self, source: Path, target: Path, compression: str, names: _ArchiveNames, depth: int
    ) -> bool:
        changed = False
        with (
            tarfile.open(source, f"r:{compression}") as original,
            _open_tar_writer(target, compression) as output,
        ):
            if self.make_transform is None:
                entries = original
                transform = self.scoped_transform(())
            else:
                entries = self.indexed_tar(original)
                transform = self.scoped_transform((item.name, item.isdir()) for item in entries)
            for item in entries:
                self.check_tar_member(item)
                name = names.plan(item.name, item.isdir())
                self.count_member(item.size)
                rewritten = copy.copy(item)
                rewritten.name = name
                rewritten.pax_headers = dict(item.pax_headers)
                if "path" in rewritten.pax_headers:
                    rewritten.pax_headers["path"] = name
                if item.isdir():
                    output.addfile(rewritten)
                    member_changed = name != item.name
                else:
                    stream = original.extractfile(item)
                    if stream is None:
                        raise ArchiveError("TAR member payload is unavailable.")
                    with stream, self.member_payload(
                        stream, item.name, item.size, depth, transform
                    ) as payload:
                        path, size, payload_changed = payload
                        rewritten.size = size
                        if "size" in rewritten.pax_headers:
                            rewritten.pax_headers["size"] = str(size)
                        with path.open("rb") as reader:
                            output.addfile(rewritten, reader)
                    member_changed = payload_changed or name != item.name
                self.changed_members += member_changed
                changed |= member_changed
        return changed


@contextmanager
def _open_tar_writer(target: Path, compression: str) -> Iterator[tarfile.TarFile]:
    if compression == "gz":
        with (
            target.open("wb") as raw,
            gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed,
        ):
            with tarfile.open(mode="w", fileobj=compressed, format=tarfile.PAX_FORMAT) as output:
                yield output
    else:
        with tarfile.open(target, f"w:{compression}", format=tarfile.PAX_FORMAT) as output:
            yield output


def _zip_extra(extra: bytes, renamed: bool) -> bytes:
    retained = bytearray()
    index = 0
    while index < len(extra):
        if index + 4 > len(extra):
            raise ArchiveError("ZIP contains an invalid extra field.")
        tag = int.from_bytes(extra[index:index + 2], byteorder="little")
        size = int.from_bytes(extra[index + 2:index + 4], byteorder="little")
        end = index + 4 + size
        if end > len(extra):
            raise ArchiveError("ZIP contains an invalid extra field.")
        # The writer recreates ZIP64 sizing. A Unicode path record would retain the old name.
        if tag != 0x0001 and not (renamed and tag == 0x7075):
            retained.extend(extra[index:end])
        index = end
    return bytes(retained)
