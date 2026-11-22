"""Patch SVG text and references while retaining the drawing's original XML."""

from __future__ import annotations

import codecs
import gzip
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote
from xml.parsers import expat
from xml.sax.saxutils import escape


SUPPORTED_SUFFIXES = frozenset({".svg", ".svgz"})
MAX_SVG_BYTES = 96 * 1024 * 1024

_SVG = "http://www.w3.org/2000/svg"
_XLINK = "http://www.w3.org/1999/xlink"
_XML = "http://www.w3.org/XML/1998/namespace"
_RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
_INKSCAPE = "http://www.inkscape.org/namespaces/inkscape"
_SODIPODI = "http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd"
_DC = {"http://purl.org/dc/elements/1.1/", "http://purl.org/dc/terms/"}
_DC_IDENTITY_FIELDS = {
    "contributor", "creator", "description", "identifier", "publisher", "relation",
    "rights", "rightsHolder", "source", "subject", "title",
}
_TEXT_ELEMENTS = {"desc", "flowPara", "flowRoot", "flowSpan", "text", "textPath", "title", "tspan"}
_TEXT_DESCENDANTS = {"a", "tref"}
_SEP = "\x1f"
_OPEN_TAG = re.compile(rb"<[^\s/>]+")
_ATTRIBUTE = re.compile(rb"[ \t\r\n]+([^\s=/>]+)[ \t\r\n]*=[ \t\r\n]*(?:\"([^\"]*)\"|'([^']*)')")
_DECLARATION = re.compile(rb"<\?xml\s+[^?]*?\bencoding\s*=\s*['\"]([A-Za-z0-9_.-]+)['\"]", re.IGNORECASE)
_SUPPORTED_ENCODINGS = {"ascii", "cp1252", "iso8859-1", "utf-16-be", "utf-16-le", "utf-8"}
_ENCODED_SEPARATOR = re.compile(r"%(?:2f|5c)", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class _Element:
    text: bool
    metadata: bool
    description: bool


def _bounded_read(stream) -> bytes:
    payload = stream.read(MAX_SVG_BYTES + 1)
    if len(payload) > MAX_SVG_BYTES:
        raise ValueError("SVG exceeds the supported document size")
    return payload


def _encoding(payload: bytes) -> tuple[str, bytes]:
    for marker, encoding in (
        (codecs.BOM_UTF8, "utf-8"), (codecs.BOM_UTF16_BE, "utf-16-be"),
        (codecs.BOM_UTF16_LE, "utf-16-le"),
    ):
        if payload.startswith(marker):
            return encoding, marker
    if payload.startswith(b"\x00<\x00?"):
        return "utf-16-be", b""
    if payload.startswith(b"<\x00?\x00"):
        return "utf-16-le", b""
    declaration = _DECLARATION.match(payload[:1024])
    if declaration is None:
        return "utf-8", b""
    try:
        encoding = codecs.lookup(declaration[1].decode("ascii")).name
    except LookupError as error:
        raise ValueError("SVG declares an unsupported character encoding") from error
    if encoding not in _SUPPORTED_ENCODINGS:
        raise ValueError("SVG declares an unsupported character encoding")
    return encoding, b""


def _name(expanded: str) -> tuple[str, str, bytes]:
    parts = expanded.split(_SEP)
    if len(parts) == 1:
        return "", expanded, expanded.encode("utf-8")
    namespace, local = parts[:2]
    original = f"{parts[2]}:{local}" if len(parts) == 3 else local
    return namespace, local, original.encode("utf-8")


def _rewrite_reference(value: str, replace: Callable[[str], str]) -> str:
    reference = value.strip()
    if not reference or reference.startswith("#") or reference.casefold().startswith(("data:", "blob:", "javascript:")):
        return value

    target, has_fragment, fragment = reference.partition("#")
    location, has_query, query = target.partition("?")
    # Preserve encoded separators. They can denote a different resource from a
    # literal slash. Decode other escapes only for relative and file references.
    local = "://" not in location or location.casefold().startswith("file://")
    if "%" in location and local and not _ENCODED_SEPARATOR.search(location):
        try:
            decoded = unquote(location, errors="strict")
        except UnicodeDecodeError:
            decoded = location
        updated = replace(decoded)
        if updated != decoded:
            location = quote(updated, safe="/:\\@")
    else:
        updated = replace(location)
        if updated != location:
            location = quote(updated, safe="/:\\@!$&'()*+,;=[]%")
    if has_query:
        query = replace(query)
    updated = location + has_query + query + has_fragment + fragment
    start = len(value) - len(value.lstrip())
    return value[:start] + updated + value[start + len(reference):]


def _patch_attributes(
    data: bytes, offset: int, pending: dict[bytes, str], patches: list[tuple[int, int, bytes]]
) -> None:
    opening = _OPEN_TAG.match(data, offset)
    if opening is None:
        raise ValueError("SVG attribute rewrite could not locate its element")
    position = opening.end()
    while pending:
        attribute = _ATTRIBUTE.match(data, position)
        if attribute is None:
            raise ValueError("SVG attribute rewrite could not locate its original value")
        position = attribute.end()
        if attribute[1] in pending:
            name = attribute[1]
            quoted = 2 if attribute[2] is not None else 3
            start, end = attribute.span(quoted)
            quote_char = chr(data[start - 1])
            entities = {
                quote_char: "&quot;" if quote_char == '"' else "&apos;",
                "\r": "&#13;", "\n": "&#10;", "\t": "&#9;",
            }
            patches.append((start, end, escape(pending.pop(name), entities).encode("utf-8")))


def _patch_xml(data: bytes, replace: Callable[[str], str]) -> bytes:
    parser = expat.ParserCreate(encoding="UTF-8", namespace_separator=_SEP)
    parser.namespace_prefixes = True
    parser.specified_attributes = True
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)

    elements: list[_Element] = []
    patches: list[tuple[int, int, bytes]] = []
    start: int | None = None
    characters: list[str] = []
    in_cdata = False
    svg_namespace: str | None = None

    def finish_text() -> None:
        nonlocal start
        if start is None:
            return
        before = "".join(characters)
        after = replace(before) if before.strip() else before
        if before != after:
            if in_cdata:
                rendered = after.replace("]]>", "]]]]><![CDATA[>")
            else:
                rendered = escape(after, {"\r": "&#13;"})
            patches.append((start, parser.CurrentByteIndex, rendered.encode("utf-8")))
        start = None
        characters.clear()

    def begin_element(expanded: str, attributes: dict[str, str]) -> None:
        nonlocal svg_namespace
        finish_text()
        namespace, local, _ = _name(expanded)
        if not elements:
            if local != "svg" or namespace not in {"", _SVG}:
                raise ValueError("XML document has no SVG root element")
            svg_namespace = namespace
        parent = elements[-1] if elements else _Element(False, False, False)
        svg = namespace == svg_namespace
        text = svg and (local in _TEXT_ELEMENTS or (parent.text and local in _TEXT_DESCENDANTS))
        metadata = parent.metadata or (svg and local == "metadata")
        description = metadata and (parent.description or namespace in _DC and local in _DC_IDENTITY_FIELDS)
        state = _Element(text, metadata, description)
        elements.append(state)

        pending: dict[bytes, str] = {}
        for expanded_attribute, value in attributes.items():
            uri, name, original = _name(expanded_attribute)
            reference = svg and (
                (uri in {"", _XLINK} and name == "href")
                or (uri == _XML and name == "base")
                or (uri == _SODIPODI and name == "absref" and local == "image")
            )
            reference |= description and uri == _RDF and name in {"about", "resource"}
            if reference:
                updated = _rewrite_reference(value, replace)
            elif (svg and (uri, name) in {("", "aria-label"), (_XLINK, "title"), (_INKSCAPE, "label")}) or (
                metadata and uri in _DC and name in _DC_IDENTITY_FIELDS
            ):
                updated = replace(value)
            else:
                continue
            if updated != value:
                pending[original] = updated
        if pending:
            _patch_attributes(data, parser.CurrentByteIndex, pending, patches)

    def end_element(_name: str) -> None:
        finish_text()
        elements.pop()

    def record_text(value: str) -> None:
        nonlocal start
        if elements and (elements[-1].text or elements[-1].description):
            if start is None:
                start = parser.CurrentByteIndex
            characters.append(value)

    def start_cdata() -> None:
        nonlocal in_cdata
        finish_text()
        in_cdata = True

    def end_cdata() -> None:
        nonlocal in_cdata
        finish_text()
        in_cdata = False

    def reject_entities(*_args) -> None:
        raise ValueError("SVG uses custom XML entities")

    parser.StartElementHandler = begin_element
    parser.EndElementHandler = end_element
    parser.CharacterDataHandler = record_text
    parser.StartCdataSectionHandler = start_cdata
    parser.EndCdataSectionHandler = end_cdata
    parser.CommentHandler = lambda _value: finish_text()
    parser.ProcessingInstructionHandler = lambda _target, _value: finish_text()
    parser.EntityDeclHandler = reject_entities
    parser.SkippedEntityHandler = reject_entities
    parser.ExternalEntityRefHandler = lambda *_args: 0
    try:
        parser.Parse(data, True)
    except expat.ExpatError as error:
        raise ValueError("SVG contains invalid or unsupported XML") from error

    if not patches:
        return data
    result = []
    last = 0
    for start, end, updated in sorted(patches):
        if start < last:
            raise ValueError("Overlapping SVG text and attribute edits")
        result.extend((data[last:start], updated))
        last = end
    result.append(data[last:])
    return b"".join(result)


def rewrite_svg(source: Path, destination: Path, replace: Callable[[str], str]) -> bool:
    """Write changed SVG data or leave the destination alone for a raw copy."""
    suffix = source.suffix.casefold()
    if suffix not in SUPPORTED_SUFFIXES:
        return False
    opener = gzip.open if suffix == ".svgz" else open
    with opener(source, "rb") as stream:
        payload = _bounded_read(stream)
    encoding, marker = _encoding(payload)
    body = payload[len(marker):] if marker else payload
    parsed = body.decode(encoding).encode("utf-8") if encoding != "utf-8" else body
    updated = _patch_xml(parsed, replace)
    if updated is parsed:
        return False
    if len(updated) > MAX_SVG_BYTES:
        raise ValueError("Rewritten SVG exceeds the supported document size")
    _patch_xml(updated, lambda text: text)
    encoded = marker + (updated.decode("utf-8").encode(encoding) if encoding != "utf-8" else updated)
    if suffix == ".svgz":
        with destination.open("wb") as stream, gzip.GzipFile(filename="", mode="wb", fileobj=stream, mtime=0) as compressed:
            compressed.write(encoded)
    else:
        destination.write_bytes(encoded)
    return True
