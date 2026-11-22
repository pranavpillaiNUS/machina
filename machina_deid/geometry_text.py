"""Rewrite designated metadata in ACIS SAT, STL, and VRML97 files."""

from __future__ import annotations

import mmap
import re
import shutil
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import BinaryIO


SUPPORTED_SUFFIXES = frozenset({".sat", ".stl", ".wrl"})

_MAX_LINE = 64 * 1024
_BUFFER_SIZE = 1024 * 1024
_BINARY_STL_HEADER = 80
_BINARY_STL_PREAMBLE = 84
_BINARY_STL_FACET = 50
_STL_COLOR = re.compile(rb"COLOR=|MATERIAL=")
_STL_NAME = re.compile(
    rb"[ \t]*(?P<kind>solid|endsolid)(?=[ \t\r\n]|$)[ \t]*"
    rb"(?P<name>[^\r\n]*?)[ \t]*(?:\r?\n)?\Z",
    re.IGNORECASE,
)
_SAT_COUNTS = re.compile(rb"[ \t]*(?:[0-9]+[ \t]+){3}[0-9]+[ \t]*\r?\n\Z")
_SAT_LENGTH = re.compile(rb"[ \t]*@?([0-9]{1,8})[ \t]")
_SAT_ATTRIBUTE = re.compile(
    rb"^[ \t]*(?:-[0-9]+[ \t]+)?string_attrib-name_attrib-gen-attrib[ \t]+"
    rb"(?:(?:\$?-?[0-9]+|[a-z_]+)[ \t]+){4,12}(?=@[0-9])",
    re.MULTILINE,
)
_SAT_ATTRIBUTE_LENGTH = re.compile(rb"[ \t\r\n]*@([0-9]{1,8})(?:\r\n|[ \t\r\n])")
_SAT_ATTRIBUTE_END = re.compile(rb"[ \t\r\n]+#(?=[ \t\r\n]|\Z)")
_SAT_IDENTITY_KEYS = frozenset({
    b"attribxacisname", b"author", b"authorname", b"company", b"companyname", b"createdby",
    b"customer", b"customername", b"designer", b"designername", b"documentname", b"email",
    b"filename", b"organization", b"owner", b"partname", b"partnumber", b"projectname",
    b"projectnumber", b"sourcepath", b"username", b"vendorname",
})
_REAL = rb"[-+]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[Ee][-+]?[0-9]+)?"
_SAT_UNITS = re.compile(rb"[ \t]*(?:" + _REAL + rb"[ \t]+){2}" + _REAL + rb"[ \t]*\r?\n\Z")
_VRML_HEADER = re.compile(rb"#VRML[ \t]+V2\.0[ \t]+utf8(?:[ \t][^\r\n]*)?(?:\r?\n|\Z)")
_VRML_TOKEN = re.compile(
    rb"[ \t\r\n,]+|\#[^\r\n]*|(?P<string>\"(?:[^\"\\]|\\[\"\\])*\")"
    rb"|(?P<punctuation>[{}\[\]])|(?P<word>[^ \t\r\n,\#{}\[\]\"\\]+)|(?P<invalid>.)"
)

_VRML_TEXT_FIELDS = {
    b"WorldInfo": {b"title", b"info"},
    b"Text": {b"string"},
    b"Anchor": {b"description"},
    b"Viewpoint": {b"description"},
}
_VRML_URL_FIELDS = {
    b"Anchor": {b"url"},
    b"AudioClip": {b"url"},
    b"Background": {b"backUrl", b"bottomUrl", b"frontUrl", b"leftUrl", b"rightUrl", b"topUrl"},
    b"ImageTexture": {b"url"},
    b"Inline": {b"url"},
    b"MovieTexture": {b"url"},
}
_VRML_INLINE_SCHEMES = ("javascript:", "ecmascript:", "vrmlscript:", "data:")

_Replace = Callable[[str], str]
_Edit = tuple[int, int, bytes]


def rewrite_geometry_text(source: Path, destination: Path, replace: _Replace) -> bool:
    """Write transformed metadata, returning false when the caller should copy the source."""
    suffix = source.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        return False

    with source.open("rb") as stream:
        if suffix == ".sat":
            edits = _sat_edits(stream, replace)
        elif suffix == ".stl":
            edits = _stl_edits(stream, source.stat().st_size, replace)
        else:
            edits = _vrml_edits(stream, replace)
        if not edits:
            return False
        with destination.open("wb") as output:
            _copy_with_edits(stream, output, edits)
    return True


def _copy_with_edits(source: BinaryIO, destination: BinaryIO, edits: Iterable[_Edit]) -> None:
    source.seek(0)
    for start, end, replacement in edits:
        remaining = start - source.tell()
        while remaining > 0:
            block = source.read(min(remaining, _BUFFER_SIZE))
            if not block:
                raise OSError("Geometry source ended before the expected metadata span")
            destination.write(block)
            remaining -= len(block)
        destination.write(replacement)
        source.seek(end)
    shutil.copyfileobj(source, destination, length=_BUFFER_SIZE)


def _replace_bytes(value: bytes, replace: _Replace) -> bytes:
    if not value:
        return value
    for encoding in ("utf-8", "cp1252"):
        try:
            text = value.decode(encoding)
        except UnicodeDecodeError:
            continue
        if not text.isprintable():
            return value
        replacement = replace(text)
        if replacement != text and replacement.strip() and replacement.isprintable():
            try:
                return replacement.encode(encoding)
            except UnicodeEncodeError:
                pass
        return value
    return value


def _sat_edits(source: BinaryIO, replace: _Replace) -> list[_Edit]:
    # Spatial's SAT Format describes the product ID as the first length-prefixed
    # value. Some writers also prefix the length with @. Preserve the dialect.
    first = source.readline(_MAX_LINE)
    if not _SAT_COUNTS.fullmatch(first):
        return []
    product_line = source.readline(_MAX_LINE)
    units_line = source.readline(_MAX_LINE)
    if not _SAT_UNITS.fullmatch(units_line) or not product_line.endswith(b"\n"):
        return []

    offset = 0
    fields = []
    for _ in range(3):
        token = _SAT_LENGTH.match(product_line, offset)
        if token is None:
            return []
        offset = token.end() + int(token[1])
        if offset > len(product_line) or product_line[offset:offset + 1] not in (b" ", b"\t", b"\r", b"\n"):
            return []
        fields.append((token, offset))
    if product_line[offset:].strip():
        return []

    token, end = fields[0]
    value = product_line[token.end():end]
    replacement = _replace_bytes(value, replace)
    edits = []
    if replacement != value:
        new_length = str(len(replacement)).encode("ascii")
        payload = new_length + product_line[token.end(1):token.end()] + replacement
        edits.append((len(first) + token.start(1), len(first) + end, payload))

    with mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ) as data:
        for record in _SAT_ATTRIBUTE.finditer(data, source.tell()):
            edit = _sat_attribute_edit(data, record.end(), replace)
            if edit is not None:
                edits.append(edit)
    return edits


def _sat_attribute_edit(data: mmap.mmap, offset: int, replace: _Replace) -> _Edit | None:
    key_field = _SAT_ATTRIBUTE_LENGTH.match(data, offset)
    if key_field is None:
        return None
    key_end = key_field.end() + int(key_field[1])
    key = re.sub(rb"[ _-]", b"", data[key_field.end():key_end].lower())
    if key not in _SAT_IDENTITY_KEYS:
        return None
    field = _SAT_ATTRIBUTE_LENGTH.match(data, key_end)
    if field is None:
        return None
    end = field.end() + int(field[1])
    if end > len(data) or not _SAT_ATTRIBUTE_END.match(data, end):
        return None
    value = data[field.end():end]
    replacement = _replace_bytes(value, replace)
    if replacement == value or any(replacement.count(char) > value.count(char) for char in b"{}$#@"):
        return None
    encoded_length = str(len(replacement)).encode("ascii")
    return field.start(1), end, encoded_length + data[field.end(1):field.end()] + replacement


def _stl_edits(source: BinaryIO, file_size: int, replace: _Replace) -> list[_Edit]:
    head = source.read(_BINARY_STL_PREAMBLE)
    if len(head) == _BINARY_STL_PREAMBLE:
        triangles = int.from_bytes(head[_BINARY_STL_HEADER:], "little")
        if file_size == _BINARY_STL_PREAMBLE + triangles * _BINARY_STL_FACET:
            return _binary_stl_edits(head[:_BINARY_STL_HEADER], replace)
    source.seek(0)
    return _ascii_stl_edits(source, replace)


def _binary_stl_edits(header: bytes, replace: _Replace) -> list[_Edit]:
    properties = _STL_COLOR.search(header)
    end = properties.start() if properties else len(header)
    prefix = header[:end]
    text, null, tail = prefix.partition(b"\0")
    if tail.strip(b"\0 "):
        return []
    raw = text.rstrip(b" ")
    if not raw or not raw.isascii() or not raw.decode("ascii").isprintable():
        return []
    replacement = _replace_bytes(raw, replace)
    if replacement == raw or not replacement.isascii() or _STL_COLOR.search(replacement):
        return []
    if len(replacement) + len(null) > end:
        return []
    if replacement.lstrip().lower().startswith(b"solid") and not raw.lstrip().lower().startswith(b"solid"):
        return []

    available = end - len(replacement)
    padding = prefix[len(raw):]
    if available <= len(padding):
        if null and null not in padding[-available:]:
            return []
        new_prefix = replacement + padding[len(padding) - available:]
    else:
        new_prefix = replacement + padding + (null or b" ") * (available - len(padding))
    return [(0, end, new_prefix)]


def _ascii_stl_edits(source: BinaryIO, replace: _Replace) -> list[_Edit]:
    edits = []
    inside = False
    while line := source.readline(_MAX_LINE):
        if len(line) == _MAX_LINE and not line.endswith(b"\n"):
            return []
        offset = source.tell() - len(line)
        name = _STL_NAME.fullmatch(line)
        if name is None:
            if not inside and line.strip():
                return []
            continue
        opening = name["kind"].lower() == b"solid"
        if opening == inside:
            return []
        inside = opening
        replacement = _replace_bytes(name["name"], replace)
        if replacement != name["name"]:
            edits.append((offset + name.start("name"), offset + name.end("name"), replacement))
    return [] if inside else edits


def _vrml_edits(source: BinaryIO, replace: _Replace) -> list[_Edit]:
    header = source.readline(_MAX_LINE)
    if not _VRML_HEADER.fullmatch(header) or not header.endswith(b"\n"):
        return []

    edits = []
    nodes: list[tuple[bytes, int]] = []
    brackets = 0
    string_list = None
    pending = None
    previous = b""
    with mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ) as data:
        for token in _VRML_TOKEN.finditer(data, len(header)):
            kind = token.lastgroup
            if kind is None:
                continue
            value = token[0]
            if kind == "invalid" or b"\0" in value:
                return []
            if kind == "string":
                field = pending if pending is not None else string_list
                if field is not None:
                    replacement = _vrml_string(value, replace, is_url=field)
                    if replacement != value:
                        edits.append((token.start(), token.end(), replacement))
                pending = None
            elif value == b"[":
                brackets += 1
                string_list = pending
                pending = None
            elif value == b"]":
                brackets -= 1
                if brackets < 0:
                    return []
                string_list = pending = None
            elif value == b"{":
                nodes.append((previous, brackets))
                pending = None
            elif value == b"}":
                if not nodes or nodes[-1][1] != brackets:
                    return []
                nodes.pop()
                pending = None
            elif nodes and nodes[-1][1] == brackets:
                node = nodes[-1][0]
                if value in _VRML_TEXT_FIELDS.get(node, ()):
                    pending = False
                elif value in _VRML_URL_FIELDS.get(node, ()):
                    pending = True
                else:
                    pending = None
            previous = value
    return [] if nodes or brackets else edits


def _vrml_string(value: bytes, replace: _Replace, *, is_url: bool) -> bytes:
    original = re.sub(rb'\\(["\\])', rb"\1", value[1:-1])
    try:
        text = original.decode("utf-8")
    except UnicodeDecodeError:
        return value
    if is_url and text.lstrip().lower().startswith(_VRML_INLINE_SCHEMES):
        return value
    replacement = replace(text) if text else text
    if replacement == text or "\0" in replacement:
        return value
    escaped = replacement.replace("\\", "\\\\").replace('"', '\\"')
    return b'"' + escaped.encode("utf-8") + b'"'
