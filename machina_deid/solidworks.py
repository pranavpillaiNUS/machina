"""Edit selected metadata in modern SolidWorks without moving native streams."""

from __future__ import annotations

import os
import re
import tempfile
import zlib
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
from xml.etree import ElementTree
from xml.parsers import expat
from xml.sax.saxutils import escape

from .native_cfb import CFB_SIGNATURE, EDITABLE_USER_PROPERTIES

if TYPE_CHECKING:
    from swformat.types import Chunk, Document

SUPPORTED_SUFFIXES = frozenset({".sldprt", ".sldasm", ".slddrw"})
MAX_METADATA_BYTES = 16 * 1024 * 1024
_MODERN_MARKER = bytes.fromhex("140006000800")

_CORE = "docProps/core.xml"
_APP = "docProps/app.xml"
_CUSTOM = "docProps/custom.xml"
_CONFIG = re.compile(r"docProps/Config-[0-9]+-Properties\.xml$")
_CORE_NS = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
_DC_NS = "http://purl.org/dc/elements/1.1/"
_APP_NS = "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
_CUSTOM_NS = "http://schemas.openxmlformats.org/officeDocument/2006/custom-properties"
_CONFIG_NS = "http://www.solidworks.com/config-properties"
_VALUE_NS = "http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"
_CORE_FIELDS = {
    *(_DC_NS + " " + name for name in ("creator", "description", "lastModifiedBy", "subject", "title")),
    *(_CORE_NS + " " + name for name in ("category", "keywords", "lastModifiedBy")),
}
_APP_FIELDS = {_APP_NS + " Company", _APP_NS + " Manager", "Company", "Manager"}
_IDENTITY_FIELDS = {
    _DC_NS + " creator": ("author", "solidworks.creator"),
    _DC_NS + " lastModifiedBy": ("author", "solidworks.last_modified_by"),
    _CORE_NS + " lastModifiedBy": ("author", "solidworks.last_modified_by"),
    _APP_NS + " Company": ("organization", "solidworks.company"),
    "Company": ("organization", "solidworks.company"),
    _APP_NS + " Manager": ("author", "solidworks.manager"),
    "Manager": ("author", "solidworks.manager"),
}
_OPEN_TAG = re.compile(rb"""<[^\s/>"']+(?:[^>"']|"[^"]*"|'[^']*')*>""")
_REFERENCE_SUFFIX = re.compile(r"\.(sldprt|sldasm|slddrw|prtdot|asmdot|drwdot)(?:$|[\s\x22])", re.I)
_FALSE_VALUES = {"", "0", "false"}

Replacement = Callable[[str], str]


class SolidWorksError(ValueError):
    """Modern SolidWorks metadata could not be rewritten safely."""


class SolidWorksUnavailable(SolidWorksError):
    """The optional modern SolidWorks backend is unavailable."""


class SolidWorksUnsupported(SolidWorksError):
    """The container or version is outside the verified write envelope."""


@dataclass(frozen=True)
class _Backend:
    read: Callable
    read_bytes: Callable
    serialize: Callable
    version: Callable
    tested_versions: frozenset[int]
    find_toc_record: Callable
    update_toc_sizes: Callable
    allocation_error: type[Exception]


@dataclass
class _Text:
    name: str
    start: int
    depth: int
    property_offset: int | None
    primary: bool
    parts: list[str] = field(default_factory=list)


def _load_backend() -> _Backend:
    try:
        from swformat.chunks.toc import _find_toc_record, update_toc_sizes
        from swformat.chunks.walker import doc_version
        from swformat.compat import TESTED_VERSIONS
        from swformat.io.reader import read_document, read_document_bytes
        from swformat.io.writer import SpanPreserveError, serialize_with_toc
    except ImportError:
        raise SolidWorksUnavailable("Install the pinned SWFormat dependency.") from None
    return _Backend(
        read_document,
        read_document_bytes,
        serialize_with_toc,
        doc_version,
        TESTED_VERSIONS,
        _find_toc_record,
        update_toc_sizes,
        SpanPreserveError,
    )


def is_modern_solidworks(source: Path) -> bool:
    """Probe the Modern SolidWorks header without loading optional dependencies."""
    if source.suffix.casefold() not in SUPPORTED_SUFFIXES:
        return False
    with source.open("rb") as stream:
        header = stream.read(64)
    return not header.startswith((CFB_SIGNATURE, b"PK\x03\x04")) and _MODERN_MARKER in header


def read_identity_metadata(source: Path) -> list[tuple[str, str, str]]:
    """Return private (kind, field, value) evidence from typed authorship fields."""
    _check_suffix(source)
    backend = _load_backend()
    try:
        document = _read_document(source, backend)
        evidence: list[tuple[str, str, str]] = []
        for chunk in document.chunks:
            if chunk.is_inline and chunk.name in {_CORE, _APP}:
                for field, _, value in _parse_xml_fields(_bounded_payload(chunk), chunk.name):
                    if field.name in _IDENTITY_FIELDS:
                        kind, label = _IDENTITY_FIELDS[field.name]
                        evidence.append((kind, label, value))
        return evidence
    except SolidWorksError:
        raise
    except (OSError, ValueError, zlib.error, expat.ExpatError):
        raise SolidWorksError("SolidWorks identity metadata could not be read.") from None


def rewrite_solidworks(
    source: Path,
    destination: Path,
    replace: Replacement,
    *,
    experimental_allow_unverified_version: bool = False,
) -> bool:
    """Write scoped metadata or return False when no eligible value changes."""
    _check_suffix(source)
    backend = _load_backend()
    try:
        if source.resolve() == destination.resolve():
            raise SolidWorksError("SolidWorks source and destination must differ.")
        original = _read_document(source, backend)
        edits = _plan_metadata_edits(original, replace)
        if not edits:
            return False
        names = {chunk.name: b"" for chunk in original.chunks}
        version = backend.version(names)
        if version is None or (
            version not in backend.tested_versions and not experimental_allow_unverified_version
        ):
            raise SolidWorksUnsupported("SolidWorks internal version has no verified write support.")
        rewritten = _render_verified(original, edits, backend)
        descriptor, filename = tempfile.mkstemp(prefix=".machina-sw-", dir=destination.parent)
        staged = Path(filename)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(rewritten)
            os.replace(staged, destination)
        finally:
            staged.unlink(missing_ok=True)
    except SolidWorksError:
        raise
    except backend.allocation_error:
        raise SolidWorksError(
            "SolidWorks pseudonym exceeds the existing compressed stream space."
        ) from None
    except (OSError, ValueError, zlib.error, expat.ExpatError, ElementTree.ParseError):
        raise SolidWorksError("SolidWorks metadata could not be read or rewritten.") from None
    return True


def _check_suffix(source: Path) -> None:
    if source.suffix.casefold() not in SUPPORTED_SUFFIXES:
        raise SolidWorksUnsupported("Unsupported SolidWorks suffix.")


def _read_document(source: Path, backend: _Backend) -> Document:
    document = backend.read(source)
    if document.fmt != "modern" or not document.chunks:
        raise SolidWorksUnsupported("Unsupported modern SolidWorks container.")
    return document


def _metadata_stream(name: str) -> bool:
    return name in {_CORE, _APP, _CUSTOM} or _CONFIG.fullmatch(name) is not None


def _bounded_payload(chunk: Chunk) -> bytes:
    inflate = zlib.decompressobj(-15)
    data = inflate.decompress(chunk.original_compressed, MAX_METADATA_BYTES + 1)
    if len(data) > MAX_METADATA_BYTES or not inflate.eof or len(data) != chunk.usz:
        raise SolidWorksError("SolidWorks metadata stream has an invalid or unsupported size.")
    return data


def _plan_metadata_edits(document: Document, replace: Replacement) -> dict[int, bytes]:
    chunks = [chunk for chunk in document.chunks if chunk.is_inline and _metadata_stream(chunk.name)]
    frequencies = Counter(chunk.name for chunk in chunks)
    edits: dict[int, bytes] = {}
    for chunk in chunks:
        original = _bounded_payload(chunk)
        changed = _rewrite_xml_metadata(original, chunk.name, replace)
        if changed != original:
            if frequencies[chunk.name] != 1:
                raise SolidWorksError("SolidWorks metadata stream has multiple competing copies.")
            edits[chunk.offset] = changed
    return edits


def _render_verified(document: Document, edits: dict[int, bytes], backend: _Backend) -> bytes:
    originals = document.chunks
    for chunk in originals:
        if chunk.offset in edits:
            chunk.modified_payload = edits[chunk.offset]
    data = backend.serialize(document)
    reopened = backend.read_bytes(data)
    if reopened.fmt != document.fmt or len(data) != len(document.data):
        raise SolidWorksError("SolidWorks stream allocations changed.")
    if len(originals) != len(reopened.chunks):
        raise SolidWorksError("SolidWorks stream inventory changed.")

    expected = bytearray(document.data)
    for before, after in zip(originals, reopened.chunks, strict=True):
        if (before.name, before.offset, before.end) != (after.name, after.offset, after.end):
            raise SolidWorksError("SolidWorks stream inventory changed.")
        if before.offset not in edits:
            continue
        if _bounded_payload(after) != edits[before.offset]:
            raise SolidWorksError("SolidWorks metadata failed its reopen check.")
        expected[before.offset:before.end] = data[before.offset:before.end]
        record = backend.find_toc_record(document.data, document, document.data[7], before.name)
        if record is not None:
            backend.update_toc_sizes(expected, record, after.csz, after.usz)

    if data != expected:
        raise SolidWorksError("SolidWorks bytes outside the approved metadata edits changed.")
    return data


def _parse_xml_fields(data: bytes, stream: str) -> list[tuple[_Text, int, str]]:
    parser = expat.ParserCreate(encoding="UTF-8", namespace_separator=" ")
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    stack: list[tuple[str, dict[str, str], int]] = []
    found: list[tuple[_Text, int, str]] = []
    active: _Text | None = None

    def start(name: str, attributes: dict[str, str]) -> None:
        nonlocal active
        if active is not None:
            active = None
        position = parser.CurrentByteIndex
        stack.append((name, attributes, position))
        depth = len(stack)
        if depth == 1 and name != _root_element(stream):
            raise SolidWorksError("SolidWorks metadata stream has an unsupported XML envelope.")
        standard_field = (stream == _CORE and name in _CORE_FIELDS) or (
            stream == _APP and name in _APP_FIELDS
        )
        if depth == 2 and standard_field:
            active = _Text(name, position, depth, None, True)
        if stream not in {_CORE, _APP} and _editable_custom_value(stack):
            active = _Text(name, position, depth, stack[2][2], depth == 4)

    def end(_name: str) -> None:
        nonlocal active
        if active is not None and active.depth == len(stack):
            value = "".join(active.parts)
            if value.strip():
                found.append((active, parser.CurrentByteIndex, value))
            active = None
        stack.pop()

    def text(value: str) -> None:
        if active is not None:
            active.parts.append(value)

    def reject_doctype(*_args: object) -> None:
        raise SolidWorksError("SolidWorks metadata contains a custom XML document type.")

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = text
    parser.StartDoctypeDeclHandler = reject_doctype
    parser.ExternalEntityRefHandler = lambda *_args: 0
    parser.Parse(data, True)
    return found


def _rewrite_xml_metadata(data: bytes, stream: str, replace: Replacement) -> bytes:
    found = _parse_xml_fields(data, stream)
    literal_properties = {
        span.property_offset
        for span, _, value in found
        if span.primary and span.property_offset is not None and _literal(value)
    }
    patches: list[tuple[int, int, bytes]] = []
    for span, end_position, original in found:
        if span.property_offset is not None and (
            span.property_offset not in literal_properties or not _literal(original)
        ):
            continue
        try:
            changed = replace(original)
        except Exception:
            raise SolidWorksError("SolidWorks property replacement failed.") from None
        if not isinstance(changed, str) or not changed.strip():
            raise SolidWorksError("Invalid SolidWorks property replacement.")
        if changed == original:
            continue
        opening = _OPEN_TAG.match(data, span.start)
        if opening is None or opening.end() > end_position:
            raise SolidWorksError("SolidWorks metadata field could not be located.")
        patches.append((opening.end(), end_position, escape(changed, {"\r": "&#13;"}).encode("utf-8")))
    if not patches:
        return data

    output: list[bytes] = []
    previous = 0
    for start_position, end_position, value in patches:
        if start_position < previous:
            raise SolidWorksError("SolidWorks metadata fields overlap.")
        output.extend((data[previous:start_position], value))
        previous = end_position
    output.append(data[previous:])
    rewritten = b"".join(output)
    if len(rewritten) > MAX_METADATA_BYTES:
        raise SolidWorksError("SolidWorks metadata exceeds its supported size.")
    ElementTree.fromstring(rewritten)
    return rewritten


def _root_element(stream: str) -> str:
    if stream == _CORE:
        return _CORE_NS + " coreProperties"
    if stream == _APP:
        return _APP_NS + " Properties"
    if _CONFIG.fullmatch(stream):
        return _CONFIG_NS + " ConfigProperties"
    return _CUSTOM_NS + " Properties"


def _editable_custom_value(stack: list[tuple[str, dict[str, str], int]]) -> bool:
    depth = len(stack)
    if depth not in {4, 5} or stack[-1][0] != _VALUE_NS + " lpstr":
        return False
    section, section_attributes, _ = stack[1]
    prop, attributes, _ = stack[2]
    if (
        section != "propertySection"
        or section_attributes.get("name") != "UserDefinedProperties"
        or prop != "property"
        or (depth == 5 and stack[3][0] != "FPVals")
        or attributes.get("TypeID", "30") not in {"30", "31"}
        or any(
            attributes.get(key, "").casefold() not in _FALSE_VALUES
            for key in ("IsEquation", "LinkToParent", "Linked")
        )
    ):
        return False
    name = "".join(letter for letter in attributes.get("name", "").casefold() if letter.isalnum())
    return name in EDITABLE_USER_PROPERTIES


def _literal(value: str) -> bool:
    normalized = value.strip().lstrip('"').casefold()
    return (
        not normalized.startswith(("=", "$prp", "sw-"))
        and "@@" not in normalized
        and _REFERENCE_SUFFIX.search(normalized) is None
    )
