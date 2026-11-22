"""Rewrite identity-bearing fields in ASCII DXF without re-serializing geometry."""

from __future__ import annotations

import codecs
import re
from collections import Counter
from collections.abc import Callable

Replacement = Callable[[str], str]
DxfRecord = tuple[int, bytes]

_DXF_VISIBLE_CODES = {
    b"TEXT": {1},
    b"ATTRIB": {1},
    b"ATTDEF": {1, 3},
    b"DIMENSION": {1},
    b"MULTILEADER": {304},
    b"MLEADER": {304},
    b"TOLERANCE": {1},
    b"ACAD_TABLE": {1, 3},
}
_DXF_HEADER_TEXT = frozenset({
    b"$TITLE", b"$SUBJECT", b"$AUTHOR", b"$KEYWORDS", b"$COMMENTS", b"$LASTSAVEDBY",
    b"$REVISIONNUMBER", b"$CUSTOMPROPERTYTAG", b"$CUSTOMPROPERTY", b"$PROJECTNAME",
    b"$HYPERLINKBASE", b"$STYLESHEET", b"$MENU",
})
_DXF_OBJECT_TYPES = frozenset({b"XRECORD", b"FIELD", b"DICTIONARYVAR"})
_DXF_OBJECT_STRING_CODES = frozenset({1, 2, 3, 4, 6, 7, *range(300, 310), *range(430, 440), *range(470, 480)})
_DXF_PATH_TYPES = {b"BLOCK", b"IMAGEDEF", b"PDFDEFINITION", b"DGNDEFINITION", b"DWFDEFINITION"}
_DXF_MTEXT_CONTROL = re.compile(r"\\(?:[AaCcFfHhQqSsTtWw][^;]*;|U\+[0-9A-Fa-f]{4}|.)|[{}]")


def split_lines(data: bytes) -> list[tuple[bytes, bytes]]:
    """Keep each original line terminator, including a missing final one."""
    lines = re.split(rb"(\r\n|\n|\r)", data)
    if len(lines) % 2:
        lines.append(b"")
    if lines[-2:] == [b"", b""]:
        del lines[-2:]
    return list(zip(lines[::2], lines[1::2]))


def dxf_encoding(records: list[DxfRecord]) -> str:
    encoding = "cp1252"
    for (code, name), (_, value) in zip(records, records[1:]):
        if code != 9:
            continue
        if name == b"$ACADVER" and re.fullmatch(rb"AC\d{4}", value) and value >= b"AC1021":
            return "utf-8"
        if name == b"$DWGCODEPAGE":
            code_page = re.fullmatch(rb"(?:ANSI_|DOS)(\d+)", value)
            if code_page is not None:
                try:
                    encoding = codecs.lookup("cp" + code_page[1].decode("ascii")).name
                except LookupError:
                    pass
    return encoding


def editable(record_type: bytes, code: int, header_name: bytes | None) -> bool:
    """Return True for the only fields the rewriter may change."""
    if header_name is not None:
        return code == 1 and header_name in _DXF_HEADER_TEXT
    return (
        code in {999, 1000}
        or code in _DXF_VISIBLE_CODES.get(record_type, ())
        or (code == 1 and record_type in _DXF_PATH_TYPES)
        or (code in _DXF_OBJECT_STRING_CODES and record_type in _DXF_OBJECT_TYPES)
    )


def _dxf_mtext(value: str, replace: Replacement) -> str:
    parts = []
    end = 0
    for command in _DXF_MTEXT_CONTROL.finditer(value):
        parts.extend([replace(value[end:command.start()]), command.group()])
        end = command.end()
    parts.append(replace(value[end:]))
    return "".join(parts)


def _parse(data: bytes) -> tuple[list[tuple[bytes, bytes]], list[DxfRecord]]:
    if data.startswith(b"AutoCAD Binary DXF"):
        raise ValueError("Binary DXF requires a binary-aware adapter")
    lines = split_lines(data)
    if len(lines) % 2:
        raise ValueError("DXF has an incomplete group code pair")
    try:
        codes = [int(line[0].removeprefix(b"\xef\xbb\xbf")) for line in lines[::2]]
    except ValueError as error:
        raise ValueError("DXF contains a malformed group code") from error
    records = [(code, line[0].strip()) for code, line in zip(codes, lines[1::2])]
    if not records or records[-1] != (0, b"EOF"):
        raise ValueError("DXF is missing its EOF marker")
    return lines, records


def read_records(data: bytes) -> list[DxfRecord]:
    """Return (group code, stripped value) pairs or raise on malformed ASCII DXF."""
    return _parse(data)[1]


def header_values(records: list[DxfRecord]) -> list[tuple[str, bytes]]:
    """Return each HEADER variable's first value in file order, keeping repeats."""
    values = []
    section = None
    for index, (code, value) in enumerate(records):
        if code == 0:
            section = None
        elif code == 2 and index and records[index - 1] == (0, b"SECTION"):
            section = value
        elif section == b"HEADER" and code == 9 and index + 1 < len(records):
            values.append((value.decode("ascii", errors="replace"), records[index + 1][1]))
    return values


def _thumbnail_indices(records: list[DxfRecord]) -> set[int]:
    """Return record indices of every THUMBNAILIMAGE section, ENDSEC included."""
    indices: set[int] = set()
    start = None
    for index, record in enumerate(records):
        if start is None:
            if record == (0, b"SECTION") and records[index + 1:index + 2] == [(2, b"THUMBNAILIMAGE")]:
                start = index
        elif record == (0, b"ENDSEC"):
            indices.update(range(start, index + 1))
            start = None
    if start is not None:
        raise ValueError("DXF thumbnail section is not terminated")
    return indices


def _normalized(records: list[DxfRecord]) -> list[tuple[int, bytes, bool]]:
    """Drop thumbnails, join MTEXT text groups, and mark each record's editability."""
    skipped = _thumbnail_indices(records)
    result: list[tuple[int, bytes, bool]] = []
    record_type = b""
    section = header_name = None
    mtext: list[bytes] | None = None
    for index, (code, value) in enumerate(records):
        if index in skipped:
            continue
        in_mtext = record_type == b"MTEXT" and code in {1, 3}
        if mtext is not None and not in_mtext:
            result.append((1, b"".join(mtext), True))
            mtext = None
        if code == 0:
            record_type = value
            section = header_name = None
        elif code == 2 and record_type == b"SECTION" and section is None:
            section = value
        elif code == 9 and section == b"HEADER":
            header_name = value
        if in_mtext:
            mtext = (mtext or []) + [value]
            continue
        result.append((code, value, code != 0 and editable(record_type, code, header_name)))
    return result


def check_invariants(before: list[DxfRecord], after: list[DxfRecord]) -> None:
    """Raise unless after differs from before only in editable string fields."""
    old, new = _normalized(before), _normalized(after)
    if len(old) != len(new):
        raise ValueError("DXF edit changed a protected field")
    for (old_code, old_value, may_change), (new_code, new_value, _) in zip(old, new):
        if old_code != new_code or (old_value != new_value and not may_change):
            raise ValueError("DXF edit changed a protected field")


def drawing_shape(records: list[DxfRecord]) -> tuple[Counter, dict[bytes, bytes]]:
    """Object types (CLASS excluded) and the handle-to-type map of drawing sections.

    ODA re-handles some OBJECTS-section dictionaries and XRECORDs on every write, so
    only ENTITIES, BLOCKS and TABLES handles are expected to survive a round trip.
    """
    types = Counter(value for code, value in records if code == 0 and value != b"CLASS")
    handles: dict[bytes, bytes] = {}
    section, record_type = None, b""
    for code, value in records:
        if code == 0:
            record_type = value
            if value == b"ENDSEC":
                section = None
        elif code == 2 and record_type == b"SECTION" and section is None:
            section = value
        elif code in (5, 105) and section in (b"ENTITIES", b"BLOCKS", b"TABLES"):
            handles[value] = record_type
    return types, handles


def rewrite_dxf_bytes(data: bytes, replace: Replacement, *, strip_thumbnail: bool = True) -> bytes:
    lines, records = _parse(data)
    encoding = dxf_encoding(records)

    def write_value(index: int, value: bytes) -> None:
        if b"\n" in value or b"\r" in value or b"\x00" in value:
            raise ValueError("Replacement contains an invalid DXF line character")
        line = index * 2 + 1
        lines[line] = (value, lines[line][1])

    rebuilt: dict[int, list[tuple[bytes, bytes]]] = {}
    dropped: set[int] = _thumbnail_indices(records) if strip_thumbnail else set()

    def rewrite_mtext(indices: list[int]) -> None:
        if not indices:
            return
        values = [lines[index * 2 + 1][0] for index in indices]
        before = b"".join(values).decode(encoding, errors="surrogateescape")
        after = _dxf_mtext(before, replace)
        if before == after:
            return
        chunks = _chunks(after, encoding)
        if any(b"\n" in chunk or b"\r" in chunk or b"\x00" in chunk for chunk in chunks):
            raise ValueError("Replacement contains an invalid DXF line character")
        first, last = indices[0], indices[-1]
        code_ending = lines[first * 2][1]
        value_ending = lines[first * 2 + 1][1]
        final_code = lines[last * 2][0] if records[last][0] == 1 else lines[last * 2][0].replace(b"3", b"1", 1)
        chunk_code = lines[first * 2][0] if records[first][0] == 3 else final_code.replace(b"1", b"3", 1)
        pairs = []
        for position, chunk in enumerate(chunks, 1):
            code_line = final_code if position == len(chunks) else chunk_code
            pairs.extend([(code_line, code_ending), (chunk, value_ending)])
        rebuilt[first] = pairs
        dropped.update(indices[1:])

    record_type = b""
    section = None
    header_name = None
    mtext_indices = []
    for index, (code, value) in enumerate(records):
        if code == 0:
            rewrite_mtext(mtext_indices)
            mtext_indices = []
            record_type = value
            section = header_name = None
        elif code == 2 and record_type == b"SECTION" and section is None:
            section = value
        elif code == 9 and section == b"HEADER":
            header_name = value
        elif record_type == b"MTEXT" and code in {1, 3}:
            mtext_indices.append(index)
        elif editable(record_type, code, header_name):
            before = lines[index * 2 + 1][0].decode(encoding, errors="surrogateescape")
            after = _dxf_mtext(before, replace) if record_type in _DXF_VISIBLE_CODES else replace(before)
            if before != after:
                updated = after.encode(encoding, errors="surrogateescape")
                if code == 1000 and len(updated) > 255:
                    raise ValueError("DXF extended data replacement exceeds 255 bytes")
                write_value(index, updated)
    rewrite_mtext(mtext_indices)
    output = []
    for index in range(len(records)):
        if index in rebuilt:
            output.extend(rebuilt[index])
        elif index not in dropped:
            output.extend(lines[index * 2:index * 2 + 2])
    result = b"".join(value + ending for value, ending in output)
    if result != data:
        check_invariants(records, read_records(result))
    return result


def _chunks(text: str, encoding: str, limit: int = 250) -> list[bytes]:
    """Split encoded MTEXT into groups of at most limit bytes.

    Boundaries fall on character boundaries and avoid whitespace at either edge of
    a group, because DXF readers commonly strip group values.
    """
    encoded = [character.encode(encoding, errors="surrogateescape") for character in text]
    chunks = []
    start = 0
    while True:
        end, size = start, 0
        while end < len(encoded) and size + len(encoded[end]) <= limit:
            size += len(encoded[end])
            end += 1
        if end == start and end < len(encoded):
            raise ValueError("DXF MTEXT character exceeds the group size limit")
        if end < len(encoded):
            split = end
            while split > start + 1 and (text[split].isspace() or text[split - 1].isspace()):
                split -= 1
            if split > start + 1:
                end = split
        chunks.append(b"".join(encoded[start:end]))
        if end >= len(encoded):
            return chunks
        start = end
