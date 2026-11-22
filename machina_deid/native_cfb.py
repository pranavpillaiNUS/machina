"""Length-preserving edits of MS-OLEPS metadata in native CAD CFB files."""

from __future__ import annotations

import codecs
import os
import shutil
import struct
import tempfile
from collections.abc import Callable
from pathlib import Path
from uuid import UUID

SUPPORTED_SUFFIXES = frozenset({".sldprt", ".sldasm", ".slddrw", ".ipt", ".iam", ".idw", ".ipn"})
CFB_SIGNATURE = bytes.fromhex("d0cf11e0a1b11ae1")
MAX_PROPERTY_STREAM_BYTES = 16 * 1024 * 1024

SUMMARY_STREAM = "\x05SummaryInformation"
DOCUMENT_SUMMARY_STREAM = "\x05DocumentSummaryInformation"
SUMMARY_FMTID = UUID("f29f85e0-4ff9-1068-ab91-08002b27b3d9").bytes_le
DOCUMENT_SUMMARY_FMTID = UUID("d5cdd502-2e9c-101b-9397-08002b2cf9ae").bytes_le
USER_DEFINED_FMTID = UUID("d5cdd505-2e9c-101b-9397-08002b2cf9ae").bytes_le
STREAM_FORMATS = {
    SUMMARY_STREAM.casefold(): ((SUMMARY_FMTID,),),
    DOCUMENT_SUMMARY_STREAM.casefold(): (
        (DOCUMENT_SUMMARY_FMTID,),
        (DOCUMENT_SUMMARY_FMTID, USER_DEFINED_FMTID),
    ),
}
EDITABLE_PROPERTY_IDS = {
    SUMMARY_FMTID: frozenset({2, 3, 4, 5, 6, 8}),
    DOCUMENT_SUMMARY_FMTID: frozenset({2, 14, 15}),
}
EDITABLE_USER_PROPERTIES = frozenset(
    """
    author category checkedby client company contact contactemail contactname
    createdby customer customername customerpartnumber designedby description
    domain drawingname drawingnumber drawnby email engineer firstname
    lastname lastsavedby manager manufacturer organization organisation
    partdescription partname partnumber phoneno phonenumber project projectname
    projectnumber reviewer title username vendor website workorder
    """.split()
)
VT_I2 = 0x02
VT_BSTR = 0x08
VT_LPSTR = 0x1E
VT_LPWSTR = 0x1F
WINDOWS_UNICODE = 1200

Replacement = Callable[[str], str]
PropertySpans = dict[int, tuple[int, int]]


class NativeCfbError(ValueError):
    """A native compound-file change cannot be made safely."""


def rewrite_native_cfb(source: Path, destination: Path, replace: Replacement) -> bool:
    """Write only changed named property streams and preserve the exact file length."""
    if source.suffix.casefold() not in SUPPORTED_SUFFIXES:
        raise NativeCfbError("Unsupported native CAD suffix.")
    try:
        if source.resolve() == destination.resolve():
            raise NativeCfbError("Native source and destination must differ.")
        with source.open("rb") as stream:
            if stream.read(len(CFB_SIGNATURE)) != CFB_SIGNATURE:
                raise NativeCfbError("Unsupported native CAD container.")

        try:
            import olefile
        except ImportError:
            raise NativeCfbError("Install olefile to edit native CAD compound metadata.") from None

        edits: dict[str, tuple[bytes, bytes]] = {}
        with olefile.OleFileIO(source, raise_defects=olefile.DEFECT_INCORRECT) as compound:
            for path in compound.listdir():
                if len(path) != 1 or path[0].casefold() not in STREAM_FORMATS:
                    continue
                name = path[0]
                if compound.get_size(name) > MAX_PROPERTY_STREAM_BYTES:
                    raise NativeCfbError("Native property stream exceeds its supported size.")
                with compound.openstream(name) as stream:
                    data = stream.read()
                changed = _rewrite_property_stream(data, name, replace)
                if changed != data:
                    edits[name] = data, changed
        if not edits:
            return False

        descriptor, filename = tempfile.mkstemp(prefix=".machina-cfb-", dir=destination.parent)
        staged = Path(filename)
        try:
            os.close(descriptor)
            shutil.copyfile(source, staged)
            original_size = staged.stat().st_size
            with olefile.OleFileIO(
                staged, write_mode=True, raise_defects=olefile.DEFECT_INCORRECT
            ) as compound:
                for name, (expected, changed) in edits.items():
                    with compound.openstream(name) as stream:
                        if stream.read() != expected:
                            raise NativeCfbError("Source metadata changed during the edit.")
                    compound.write_stream(name, changed)
            if staged.stat().st_size != original_size:
                raise NativeCfbError("Native compound file length changed.")
            with olefile.OleFileIO(staged, raise_defects=olefile.DEFECT_INCORRECT) as compound:
                for name, (_, changed) in edits.items():
                    with compound.openstream(name) as stream:
                        if stream.read() != changed:
                            raise NativeCfbError("Edited native metadata failed verification.")
            os.replace(staged, destination)
        finally:
            staged.unlink(missing_ok=True)
    except NativeCfbError:
        raise
    except (EOFError, IndexError, OSError, OverflowError, TypeError, ValueError, struct.error):
        raise NativeCfbError("Native CAD compound metadata could not be read or written.") from None
    return True


def _rewrite_property_stream(data: bytes, name: str, replace: Replacement) -> bytes:
    formats = STREAM_FORMATS[name.casefold()]
    if len(data) < 28 or len(data) > MAX_PROPERTY_STREAM_BYTES:
        raise NativeCfbError("Native property stream has an unsupported length.")
    byte_order, version = struct.unpack_from("<HH", data)
    count = struct.unpack_from("<I", data, 24)[0]
    header_end = 28 + 20 * count
    if byte_order != 0xFFFE or version not in {0, 1} or count not in {1, 2} or header_end > len(data):
        raise NativeCfbError("Invalid native property stream header.")
    sections = tuple(struct.iter_unpack("<16sI", data[28:header_end]))
    if tuple(fmtid for fmtid, _ in sections) not in formats:
        raise NativeCfbError("Unsupported native property stream type.")

    output = bytearray(data)
    previous_end = header_end
    for fmtid, start in sorted(sections, key=lambda section: section[1]):
        if start < previous_end or start % 4 or start + 8 > len(data):
            raise NativeCfbError("Invalid native property section offset.")
        size, property_count = struct.unpack_from("<II", data, start)
        end = start + size
        values_start = start + 8 + 8 * property_count
        if end > len(data) or values_start > end:
            raise NativeCfbError("Invalid native property section length.")
        spans = _property_spans(data, start, values_start, end)
        if fmtid == USER_DEFINED_FMTID:
            properties = _user_property_ids(data, spans)
        else:
            properties = EDITABLE_PROPERTY_IDS[fmtid]
        for property_id in properties & spans.keys():
            _rewrite_property(data, output, spans, property_id, replace)
        previous_end = end
    return bytes(output)


def _property_spans(data: bytes, start: int, values_start: int, end: int) -> PropertySpans:
    positions = sorted(
        (start + offset, property_id)
        for property_id, offset in struct.iter_unpack("<II", memoryview(data)[start + 8:values_start])
    )
    spans: PropertySpans = {}
    for index, (offset, property_id) in enumerate(positions):
        limit = end if index + 1 == len(positions) else positions[index + 1][0]
        if offset < values_start or offset % 4 or offset + 4 > limit or limit > end:
            raise NativeCfbError("Invalid native property value offset.")
        if property_id in spans:
            raise NativeCfbError("Duplicate native property identifiers.")
        spans[property_id] = offset, limit
    return spans


def _code_page(data: bytes, spans: PropertySpans) -> int:
    if 1 not in spans:
        raise NativeCfbError("Native property section lacks an encoding.")
    start, end = spans[1]
    if end - start < 8:
        raise NativeCfbError("Invalid native property encoding.")
    kind, code_page, padding = struct.unpack_from("<IHH", data, start)
    if kind != VT_I2 or padding:
        raise NativeCfbError("Invalid native property encoding.")
    return code_page


def _string_encoding(code_page: int) -> tuple[str, int]:
    if code_page == WINDOWS_UNICODE:
        return "utf-16-le", 2
    if code_page == 65001:
        return "utf-8", 1
    if code_page == 10000:
        return "mac_roman", 1
    try:
        return codecs.lookup(f"cp{code_page}").name, 1
    except LookupError:
        raise NativeCfbError("Unsupported native property encoding.") from None


def _user_property_ids(data: bytes, spans: PropertySpans) -> set[int]:
    if 0 not in spans:
        return set()
    position, end = spans[0]
    count = struct.unpack_from("<I", data, position)[0]
    position += 4
    if count > (end - position) // 9:
        raise NativeCfbError("Invalid native custom property dictionary.")
    encoding, width = _string_encoding(_code_page(data, spans))
    ids: set[int] = set()
    for _ in range(count):
        if position + 8 > end:
            raise NativeCfbError("Invalid native custom property dictionary.")
        property_id, length = struct.unpack_from("<II", data, position)
        position += 8
        size = length * width
        next_position = position + (_align4(size) if width == 2 else size)
        if not size or next_position > end or not 2 <= property_id <= 0x7FFFFFFF:
            raise NativeCfbError("Invalid native custom property dictionary.")
        key = _decode_string(data[position:position + size], encoding, width)
        normalized = "".join(character for character in key.casefold() if character.isalnum())
        if normalized in EDITABLE_USER_PROPERTIES:
            ids.add(property_id)
        position = next_position
    return ids


def _rewrite_property(
    data: bytes, output: bytearray, spans: PropertySpans, property_id: int, replace: Replacement
) -> None:
    start, end = spans[property_id]
    kind, padding = struct.unpack_from("<HH", data, start)
    if kind not in {VT_BSTR, VT_LPSTR, VT_LPWSTR}:
        return
    if padding or end - start < 8:
        raise NativeCfbError("Invalid native string property length.")
    if kind == VT_LPWSTR:
        encoding, width, length_scale = "utf-16-le", 2, 2
    else:
        encoding, width = _string_encoding(_code_page(data, spans))
        length_scale = 1
    length = struct.unpack_from("<I", data, start + 4)[0]
    if length == 0:
        return
    size = length * length_scale
    characters = start + 8
    available = _align4(size)
    if characters + available > end:
        raise NativeCfbError("Invalid native string property length.")
    original = _decode_string(data[characters:characters + size], encoding, width)
    rewritten = "\x00".join(_replace_text(part, replace) for part in original.split("\x00"))
    if rewritten == original:
        return
    try:
        content = rewritten.encode(encoding) + b"\x00" * width
    except UnicodeEncodeError:
        raise NativeCfbError("Native pseudonym is incompatible with the stored encoding.") from None
    if len(content) > available:
        raise NativeCfbError("Native pseudonym exceeds the existing property space.")
    struct.pack_into("<I", output, start + 4, len(content) // length_scale)
    output[characters:characters + available] = content.ljust(available, b"\x00")


def _decode_string(data: bytes, encoding: str, width: int) -> str:
    if len(data) % width or not data.endswith(b"\x00" * width):
        raise NativeCfbError("Invalid native property string termination.")
    try:
        return data[:-width].decode(encoding)
    except UnicodeDecodeError:
        raise NativeCfbError("Native property string has invalid encoding.") from None


def _replace_text(value: str, replace: Replacement) -> str:
    try:
        changed = replace(value) if value else value
    except Exception:
        raise NativeCfbError("Native property replacement failed.") from None
    if not isinstance(changed, str) or "\x00" in changed or (value.strip() and not changed.strip()):
        raise NativeCfbError("Invalid native property replacement.")
    return changed


def _align4(length: int) -> int:
    return (length + 3) & ~3
