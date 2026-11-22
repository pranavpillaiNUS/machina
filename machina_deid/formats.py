"""Rewrite known text fields without re-serializing CAD geometry."""

from __future__ import annotations

import codecs
import difflib
import hashlib
import re
import tempfile
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from functools import cmp_to_key
from pathlib import Path, PurePosixPath

Replacement = Callable[[str], str]
AttachmentTransform = Callable[[str, Path, Path], bool]
SUPPORTED_SUFFIXES = frozenset({".stp", ".step", ".dxf", ".igs", ".iges", ".pdf"})

_STEP_TOKEN = re.compile(
    rb"(?P<comment>/\*.*?\*/)|(?P<schema>\bFILE_SCHEMA\s*\(.*?\)\s*;)|"
    rb"(?P<string>'(?:[^']|'')*')",
    re.DOTALL | re.IGNORECASE,
)
_STEP_ESCAPE = re.compile(
    r"\\X2\\([0-9A-F]+)\\X0\\|\\X4\\([0-9A-F]+)\\X0\\|\\X\\([0-9A-F]{2})|\\(\\)",
    re.IGNORECASE,
)
_DXF_VISIBLE_CODES = {
    b"TEXT": {1},
    b"ATTRIB": {1},
    b"ATTDEF": {1, 3},
    b"DIMENSION": {1},
    b"MULTILEADER": {304},
    b"MLEADER": {304},
    b"TOLERANCE": {1},
}
_DXF_PATH_TYPES = {b"BLOCK", b"IMAGEDEF", b"PDFDEFINITION", b"DGNDEFINITION", b"DWFDEFINITION"}
_DXF_MTEXT_CONTROL = re.compile(r"\\(?:[AaCcFfHhQqSsTtWw][^;]*;|U\+[0-9A-Fa-f]{4}|.)|[{}]")
_IGES_HOLLERITH = re.compile(rb"([0-9]+)[Hh]")
_PDF_TOKEN = re.compile(r"\w+|[^\w\s]|\s+")
_PDF_META_FIELDS = {"title", "author", "subject", "keywords", "creator", "producer"}
_PDF_ANNOT_FIELDS = {"content", "title", "subject"}
_PDF_XMP_FIELDS = {
    "Author", "Company", "CreatorTool", "Keywords", "Manager", "Producer", "Title",
    "creator", "description", "publisher", "rights", "subject", "title",
}
_PDF_FILE_NAMES = ("UF", "F", "DOS", "Mac", "Unix")
_PDF_ATTACHMENT_LIMIT = 32 * 1024 * 1024
_PDF_ATTACHMENT_TOTAL = 128 * 1024 * 1024
_PDF_ATTACHMENT_GRAPH_LIMIT = 200_000
_PDF_PROTECTED_ATTACHMENTS = {"EncryptedPayload", "C2PA_Manifest", "FormData", "Schema"}


def rewrite_file(
    source: Path, destination: Path, replace: Replacement, *, attachment_transform: AttachmentTransform | None = None
) -> bool:
    """Write an edited file and return True. Leave the destination alone if unchanged.

    Callers should provide a staging destination and handle copying on False.
    An adapter raises on unsupported features or a failed rewrite where it cannot
    safely determine that the source would be unchanged.
    """
    suffix = source.suffix.casefold()
    if suffix == ".pdf":
        return _rewrite_pdf(source, destination, replace, attachment_transform)

    if suffix not in SUPPORTED_SUFFIXES:
        return False
    original = source.read_bytes()
    if suffix in {".stp", ".step"}:
        rewritten = _rewrite_step(original, replace)
    elif suffix == ".dxf":
        rewritten = _rewrite_dxf(original, replace)
    else:
        rewritten = _rewrite_iges(original, replace)
    if rewritten == original:
        return False
    destination.write_bytes(rewritten)
    return True


def _step_decode(body: bytes) -> str:
    value = body.decode("utf-8", errors="surrogateescape").replace("''", "'")
    parts = []
    end = 0
    for match in _STEP_ESCAPE.finditer(value):
        literal = value[end:match.start()]
        if "\\" in literal:
            raise ValueError("Unrecognized STEP character directive")
        parts.append(literal)
        if match.lastindex == 4:
            parts.append("\\")
        else:
            encoding = ("utf-16-be", "utf-32-be", "latin-1")[match.lastindex - 1]
            parts.append(bytes.fromhex(match.group(match.lastindex)).decode(encoding))
        end = match.end()
    literal = value[end:]
    if "\\" in literal:
        raise ValueError("Unrecognized STEP character directive")
    return "".join([*parts, literal])


def _step_encode(value: str) -> bytes:
    parts = []
    for char in value:
        if char in {"'", "\\"}:
            parts.append(char * 2)
        elif 0xDC80 <= ord(char) <= 0xDCFF or " " <= char <= "~":
            parts.append(char)
        else:
            encoded = char.encode("utf-16-be").hex().upper()
            parts.append(f"\\X2\\{encoded}\\X0\\")
    return "".join(parts).encode("utf-8", errors="surrogateescape")


def _rewrite_step(data: bytes, replace: Replacement) -> bytes:
    if not data.lstrip(b"\xef\xbb\xbf \t\r\n").upper().startswith(b"ISO-10303-21;"):
        raise ValueError("Invalid STEP exchange header")
    if re.search(rb"(?im)^\s*SIGNATURE\s*;", data):
        raise ValueError("Signed STEP exchange files require re-signing")

    def rewrite_token(match: re.Match[bytes]) -> bytes:
        token = match.group()
        if match.lastgroup == "schema":
            return token
        if match.lastgroup == "comment":
            comment = token[2:-2].decode("utf-8", errors="surrogateescape")
            result = replace(comment)
            if "*/" in result:
                raise ValueError("Replacement would terminate a STEP comment")
            return b"/*" + result.encode("utf-8", errors="surrogateescape") + b"*/"
        try:
            original = _step_decode(token[1:-1])
        except ValueError:
            return token
        updated = replace(original)
        return b"'" + _step_encode(updated) + b"'" if updated != original else token

    return _STEP_TOKEN.sub(rewrite_token, data)


def _split_lines(data: bytes) -> list[tuple[bytes, bytes]]:
    """Keep each original line terminator, including a missing final one."""
    lines = re.split(rb"(\r\n|\n|\r)", data)
    if len(lines) % 2:
        lines.append(b"")
    if lines[-2:] == [b"", b""]:
        del lines[-2:]
    return list(zip(lines[::2], lines[1::2]))


def _dxf_encoding(records: list[tuple[int, bytes]]) -> str:
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


def _dxf_mtext(value: str, replace: Replacement) -> str:
    parts = []
    end = 0
    for command in _DXF_MTEXT_CONTROL.finditer(value):
        parts.extend([replace(value[end:command.start()]), command.group()])
        end = command.end()
    parts.append(replace(value[end:]))
    return "".join(parts)


def _rewrite_dxf(data: bytes, replace: Replacement) -> bytes:
    if data.startswith(b"AutoCAD Binary DXF"):
        raise ValueError("Binary DXF requires a binary-aware adapter")
    lines = _split_lines(data)
    if len(lines) % 2:
        raise ValueError("DXF has an incomplete group code pair")
    try:
        codes = [int(line[0].removeprefix(b"\xef\xbb\xbf")) for line in lines[::2]]
    except ValueError as error:
        raise ValueError("DXF contains a malformed group code") from error
    records = [(code, line[0].strip()) for code, line in zip(codes, lines[1::2])]
    if not records or records[-1] != (0, b"EOF"):
        raise ValueError("DXF is missing its EOF marker")
    encoding = _dxf_encoding(records)

    def write_value(index: int, value: bytes) -> None:
        if b"\n" in value or b"\r" in value or b"\x00" in value:
            raise ValueError("Replacement contains an invalid DXF line character")
        line = index * 2 + 1
        lines[line] = (value, lines[line][1])

    def rewrite_mtext(indices: list[int]) -> None:
        if not indices:
            return
        values = [lines[index * 2 + 1][0] for index in indices]
        before = b"".join(values).decode(encoding, errors="surrogateescape")
        after = _dxf_mtext(before, replace)
        if before == after:
            return
        payload = after.encode(encoding, errors="surrogateescape")
        # Group 3 chunks precede a final group 1. Keep existing group structure.
        limit = 250 * len(indices)
        if len(payload) > limit:
            raise ValueError("DXF MTEXT replacement exceeds the existing chunk capacity")
        # Preserve all preceding group 3 chunks at their original byte widths.
        for index, old in zip(indices[:-1], values[:-1]):
            if len(payload) <= len(old):
                raise ValueError("DXF MTEXT replacement does not fit the existing chunk structure")
            chunk, payload = payload[:len(old)], payload[len(old):]
            write_value(index, chunk)
        if len(payload) > 250:
            raise ValueError("DXF MTEXT final group would exceed 250 bytes")
        write_value(indices[-1], payload)

    record_type = b""
    mtext_indices = []
    for index, (code, value) in enumerate(records):
        if code == 0:
            rewrite_mtext(mtext_indices)
            mtext_indices = []
            record_type = value
        elif record_type == b"MTEXT" and code in {1, 3}:
            mtext_indices.append(index)
        elif code in {999, 1000} or code in _DXF_VISIBLE_CODES.get(record_type, ()) or (
            code == 1 and record_type in _DXF_PATH_TYPES
        ):
            before = lines[index * 2 + 1][0].decode(encoding, errors="surrogateescape")
            after = _dxf_mtext(before, replace) if record_type in _DXF_VISIBLE_CODES else replace(before)
            if before != after:
                updated = after.encode(encoding, errors="surrogateescape")
                if code == 1000 and len(updated) > 255:
                    raise ValueError("DXF extended data replacement exceeds 255 bytes")
                write_value(index, updated)
    return b"".join(value + ending for value, ending in lines)


def _rewrite_iges(data: bytes, replace: Replacement) -> bytes:
    lines = _split_lines(data)
    if not lines or any(len(line) != 80 or line[72:73] not in b"SGDPT" for line, _ in lines):
        raise ValueError("IGES adapter requires 80-column fixed-format records")
    globals_at = [index for index, (line, _) in enumerate(lines) if line[72:73] == b"G"]
    if not globals_at or lines[-1][0][72:73] != b"T":
        raise ValueError("IGES is missing its Global or Terminate section")
    global_data = b"".join(lines[index][0][:72] for index in globals_at)
    # The first two parameters may define arbitrary delimiters. Preserve those
    # cases byte-for-byte until the adapter can validate them end to end.
    if not re.match(rb"(?:1[Hh],)?,(?:1[Hh];)?,", global_data):
        raise ValueError("IGES uses unsupported Global parameter delimiters")
    identity_fields = {2, 3, 11, 20, 21}
    chunks = []
    position = 0
    field = 0
    while position < len(global_data):
        start = position
        while global_data[position:position + 1] == b" ":
            position += 1
        token = _IGES_HOLLERITH.match(global_data, position)
        if token is not None:
            end = token.end() + int(token[1])
            if end > len(global_data):
                raise ValueError("IGES Hollerith string exceeds its section")
            before = global_data[token.end():end].decode("ascii", errors="surrogateescape")
            after = replace(before) if field in identity_fields else before
            if before == after:
                chunks.append(global_data[start:end])
            else:
                encoded = after.encode("ascii", errors="surrogateescape")
                if any(char in encoded for char in (b"\r", b"\n", b"\x00")):
                    raise ValueError("Replacement contains an invalid IGES record character")
                chunks.extend([global_data[start:position], str(len(encoded)).encode(), b"H", encoded])
            start = end
        delimiter = re.search(rb"[,;]", global_data[start:])
        if delimiter is None:
            raise ValueError("IGES Global section is missing a delimiter")
        position = start + delimiter.end()
        chunks.append(global_data[start:position])
        if delimiter.group() == b";":
            break
        field += 1
    else:
        raise ValueError("IGES Global section has no record terminator")
    if global_data[position:].strip():
        raise ValueError("IGES Global section continues after its record delimiter")

    rebuilt = b"".join(chunks)
    if len(rebuilt) > len(global_data):
        terminator, ending = lines[-1]
        counter = re.fullmatch(rb"G([ 0-9]{7})", terminator[8:16])
        if counter is None or not counter[1].strip() or int(counter[1]) != len(globals_at):
            raise ValueError("IGES Terminate section has an invalid Global record count")
        if globals_at != list(range(globals_at[0], globals_at[-1] + 1)):
            raise ValueError("IGES Global records are not contiguous")
        required = (len(rebuilt) + 71) // 72
        lines[-1] = (terminator[:8] + f"G{required:7d}".encode("ascii") + terminator[16:], ending)
        insertion = globals_at[-1] + 1
        added = [(b" " * 72 + f"G{sequence:7d}".encode("ascii"), lines[globals_at[-1]][1])
                 for sequence in range(len(globals_at) + 1, required + 1)]
        lines[insertion:insertion] = added
        globals_at.extend(range(insertion, insertion + len(added)))
    rebuilt = rebuilt.ljust(len(globals_at) * 72)
    for offset, index in enumerate(globals_at):
        line, ending = lines[index]
        lines[index] = (rebuilt[offset * 72:(offset + 1) * 72] + line[72:], ending)
    for index, (line, ending) in enumerate(lines):
        if line[72:73] != b"S":
            break
        before = line[:72].rstrip().decode("ascii", errors="surrogateescape")
        after = replace(before)
        if before != after:
            encoded = after.encode("ascii", errors="surrogateescape")
            if len(encoded) > 72 or any(char in encoded for char in (b"\r", b"\n", b"\x00")):
                raise ValueError("IGES Start replacement does not fit its record")
            lines[index] = (encoded.ljust(72) + line[72:], ending)
    return b"".join(line + ending for line, ending in lines)


def _pdf_xmp(xml: str, replace: Replacement) -> str:
    parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True, insert_pis=True))
    root = ET.fromstring(xml, parser=parser)
    changed = False

    def rewrite_element(element: ET.Element, inherited: bool = False) -> None:
        nonlocal changed
        if not isinstance(element.tag, str):
            return
        selected = inherited or element.tag.rsplit("}", 1)[-1] in _PDF_XMP_FIELDS
        if selected and element.text:
            after = replace(element.text)
            changed |= after != element.text
            element.text = after
        for attribute, before in element.attrib.items():
            if attribute.rsplit("}", 1)[-1] in _PDF_XMP_FIELDS:
                after = replace(before)
                changed |= after != before
                element.set(attribute, after)
        for child in element:
            rewrite_element(child, selected)

    rewrite_element(root)
    if not changed:
        return xml
    result = ET.tostring(root, encoding="unicode")
    packet = re.findall(r"<\?xpacket\s[^?]*\?>", xml)
    if len(packet) == 2:
        return f"{packet[0]}\n{result}\n{packet[1]}"
    return result


def _pdf_hunks(before: str, after: str):
    """Align edits to lexical tokens so replacements stay together visually."""
    old = list(_PDF_TOKEN.finditer(before))
    new = list(_PDF_TOKEN.finditer(after))
    matcher = difflib.SequenceMatcher(
        None, [word.group() for word in old], [word.group() for word in new], autojunk=False
    )
    edits = []
    for tag, start, end, updated_start, updated_end in matcher.get_opcodes():
        if tag == "equal":
            continue
        replacement = "".join(word.group() for word in new[updated_start:updated_end])
        if tag == "insert":
            if start == len(old):
                start -= 1
                replacement = old[start].group() + replacement
            else:
                end += 1
                replacement += old[start].group()
        edits.append((old[start].start(), old[end - 1].end(), replacement))
    return edits


@dataclass(frozen=True)
class _PdfEdit:
    removed: frozenset[int]
    bounds: tuple[float, float, float, float]
    origin: tuple[float, float]
    text: str
    font: str
    size: float
    rotation: int
    color: tuple[float, float, float]


def _pdf_letters(pymupdf, page):
    # Omitting image data from rawdict keeps high-resolution drawings cheap to scan.
    flags = pymupdf.TEXTFLAGS_RAWDICT & ~pymupdf.TEXT_PRESERVE_IMAGES
    letters = []
    lines = []
    for block in page.get_text("rawdict", flags=flags)["blocks"]:
        for line in block.get("lines", []):
            start = len(letters)
            for span in line["spans"]:
                letters.extend((char, span) for char in span["chars"])
            if start < len(letters):
                lines.append((start, len(letters), line["dir"]))
    return letters, lines


def _pdf_font(flags: int) -> str:
    if flags & 8:
        normal, italic, bold, bold_italic = "cour", "coit", "cobo", "cobi"
    elif flags & 4:
        normal, italic, bold, bold_italic = "tiro", "tiit", "tibo", "tibi"
    else:
        normal, italic, bold, bold_italic = "helv", "heit", "hebo", "hebi"
    return (normal, italic, bold, bold_italic)[bool(flags & 2) + 2 * bool(flags & 16)]


def _pdf_make_edit(pymupdf, letters, start, end, replacement, rotation) -> _PdfEdit:
    glyphs = [char for char, _ in letters[start:end]]
    first, span = letters[start]
    font = _pdf_font(span["flags"])
    size = span["size"]
    boxes = [pymupdf.Rect(char["bbox"]) for char in glyphs]
    bounds = pymupdf.Rect(boxes[0])
    redaction = pymupdf.Rect()
    for box in boxes:
        bounds |= box
        # Redactions remove intersecting glyph boxes. Target their centers to
        # avoid deleting adjacent characters merely touching the full text box.
        inset_x = box.width * 0.25
        inset_y = box.height * 0.25
        redaction |= pymupdf.Rect(box.x0 + inset_x, box.y0 + inset_y, box.x1 - inset_x, box.y1 - inset_y)
    if redaction.is_empty:
        raise ValueError("PDF text match has empty geometry")

    if replacement:
        if not replacement.isascii() or not replacement.isprintable():
            raise ValueError("PDF replacement text requires a font with broader character coverage")
        width = bounds.height if rotation in {90, 270} else bounds.width
        unit_width = pymupdf.get_text_length(replacement, fontname=font, fontsize=1)
        if unit_width > 0:
            size = min(size, width / unit_width)
        if size < span["size"] * 0.45:
            raise ValueError("PDF pseudonym would make the drawing label unreadably small")

    return _PdfEdit(
        frozenset(range(start, end)), tuple(redaction), first["origin"], replacement, font, size,
        rotation, pymupdf.sRGB_to_pdf(span["color"]),
    )


def _pdf_rewrite_page(pymupdf, page, replace: Replacement) -> bool:
    letters, lines = _pdf_letters(pymupdf, page)
    edits = []
    for line_start, line_end, direction in lines:
        before = "".join(char["c"] for char, _ in letters[line_start:line_end])
        after = replace(before)
        if before == after:
            continue
        rotation_by_axis = {(1, 0): 0, (0, -1): 90, (-1, 0): 180, (0, 1): 270}
        axis = tuple(round(coordinate, 3) for coordinate in direction)
        if axis not in rotation_by_axis or any(len(char["c"]) != 1 for char, _ in letters[line_start:line_end]):
            raise ValueError("PDF text match uses unsupported glyph layout")
        for start, end, replacement in _pdf_hunks(before, after):
            edits.append(_pdf_make_edit(
                pymupdf, letters, line_start + start, line_start + end, replacement, rotation_by_axis[axis]
            ))

    if not edits:
        return False
    if any(annot.type[0] == pymupdf.PDF_ANNOT_REDACT for annot in page.annots()):
        raise ValueError("PDF page contains unapplied source redactions")

    links = [pymupdf.Rect(link["from"]) for link in page.get_links()]
    boxes = [pymupdf.Rect(char["bbox"]) for char, _ in letters]
    removed = set()
    for edit in edits:
        area = pymupdf.Rect(edit.bounds)
        if removed.intersection(edit.removed):
            raise ValueError("PDF text replacements overlap")
        if any(area.intersects(link) for link in links):
            raise ValueError("PDF text replacement overlaps a link annotation")
        if any(area.intersects(box) for index, box in enumerate(boxes) if index not in edit.removed):
            raise ValueError("PDF redaction would affect adjacent text")
        removed.update(edit.removed)

    for edit in edits:
        page.add_redact_annot(edit.bounds, fill=False, cross_out=False)
    page.apply_redactions(images=0, graphics=0, text=0)
    for edit in edits:
        if edit.text:
            page.insert_text(
                edit.origin, edit.text, fontname=edit.font, fontsize=edit.size, rotate=edit.rotation, color=edit.color
            )

    expected = Counter(char["c"] for index, (char, _) in enumerate(letters) if index not in removed)
    for edit in edits:
        expected.update(edit.text)
    actual = Counter(char["c"] for char, _ in _pdf_letters(pymupdf, page)[0])
    # MuPDF can synthesize spaces differently after a content stream edit.
    for counter in (expected, actual):
        del counter[" "]
    if actual != expected:
        raise ValueError("PDF text validation found missing or unexpected glyphs")
    return True


def _pdf_objects(mupdf, catalog):
    """Walk reachable dictionaries once without reading PDF content streams."""
    pending = [catalog]
    indirect = set()
    seen = 0
    while pending:
        obj = pending.pop()
        seen += 1
        if seen > _PDF_ATTACHMENT_GRAPH_LIMIT:
            raise ValueError("PDF object graph exceeds the attachment scan limit")
        if mupdf.pdf_is_indirect(obj):
            xref = mupdf.pdf_to_num(obj)
            if xref in indirect:
                continue
            indirect.add(xref)
            obj = mupdf.pdf_resolve_indirect_chain(obj)
        if mupdf.pdf_is_dict(obj):
            yield obj
            pending.extend(mupdf.pdf_dict_get_val(obj, index) for index in range(mupdf.pdf_dict_len(obj)))
        elif mupdf.pdf_is_array(obj):
            pending.extend(mupdf.pdf_array_get(obj, index) for index in range(mupdf.pdf_array_len(obj)))


def _attachment_basename(value: str) -> str:
    name = PurePosixPath(value.replace("\\", "/")).name
    if name in {"", ".", ".."} or len(name) > 255 or any(ord(char) < 32 or char in '<>:"/\\|?*' for char in name):
        return ""
    return name


def _renamed_attachment(value: str, replace: Replacement) -> str:
    if not value or _attachment_basename(value) != value:
        return value
    updated = replace(value)
    if _attachment_basename(updated) != updated or not updated:
        return value
    if PurePosixPath(updated).suffix.casefold() != PurePosixPath(value).suffix.casefold():
        return value
    return updated


def _pdf_string(mupdf, obj, key: str) -> str:
    value = mupdf.pdf_dict_gets(obj, key)
    return mupdf.pdf_to_text_string(value) if mupdf.pdf_is_string(value) else ""


def _pdf_rename_attachment_keys(mupdf, tree, replace: Replacement) -> bool:
    """Plan a name-tree rename and commit only if existing leaf ranges still fit."""
    plans = []
    visited = set()
    renamed = set()
    changed = False

    def compare(a, b):
        return mupdf.pdf_objcmp_resolve(a, b)

    def tree_limits(node, depth):
        nonlocal changed
        xref = mupdf.pdf_to_num(node)
        if depth > 40 or (xref and xref in visited) or not mupdf.pdf_is_dict(node):
            raise ValueError("Unsupported embedded PDF name tree")
        if xref:
            visited.add(xref)
        names = mupdf.pdf_dict_gets(node, "Names")
        kids = mupdf.pdf_dict_gets(node, "Kids")
        limits = mupdf.pdf_dict_gets(node, "Limits")
        entries = None
        if mupdf.pdf_is_array(names) and mupdf.pdf_is_null(kids):
            count = mupdf.pdf_array_len(names)
            if count % 2 or not count:
                raise ValueError("Invalid embedded PDF name tree entry count")
            entries = []
            for index in range(0, count, 2):
                key = mupdf.pdf_array_get(names, index)
                if not mupdf.pdf_is_string(key):
                    raise ValueError("Non-string embedded PDF name tree key")
                before = mupdf.pdf_to_text_string(key)
                after = _renamed_attachment(before, replace)
                if after in renamed:
                    raise ValueError("Embedded PDF names collide")
                renamed.add(after)
                if before != after:
                    key = mupdf.pdf_new_text_string(after)
                    changed = True
                entries.append((key, mupdf.pdf_array_get(names, index + 1)))
            entries.sort(key=cmp_to_key(lambda a, b: compare(a[0], b[0])))
            low, high = entries[0][0], entries[-1][0]
        elif mupdf.pdf_is_array(kids) and mupdf.pdf_is_null(names) and mupdf.pdf_array_len(kids):
            ranges = [tree_limits(mupdf.pdf_array_get(kids, index), depth + 1) for index in range(mupdf.pdf_array_len(kids))]
            if any(compare(left[1], right[0]) >= 0 for left, right in zip(ranges, ranges[1:])):
                raise ValueError("Embedded PDF names would move across name-tree leaves")
            low, high = ranges[0][0], ranges[-1][1]
        else:
            raise ValueError("Unsupported embedded PDF name tree layout")
        if not mupdf.pdf_is_null(limits) and mupdf.pdf_array_len(limits) != 2:
            raise ValueError("Invalid embedded PDF name tree limits")
        plans.append((names, entries, limits, low, high))
        return low, high

    tree_limits(tree, 0)
    if not changed:
        return False
    for names, entries, limits, low, high in plans:
        if entries is not None:
            for index, (key, spec) in enumerate(entries):
                mupdf.pdf_array_put(names, index * 2, key)
                mupdf.pdf_array_put(names, index * 2 + 1, spec)
        if mupdf.pdf_is_array(limits):
            mupdf.pdf_array_put(limits, 0, low)
            mupdf.pdf_array_put(limits, 1, high)
    return True


def _pdf_write_attachment_payload(mupdf, stream, staged: Path, limit: int) -> int | None:
    source = mupdf.pdf_open_stream(stream)
    buffer = bytearray(min(limit + 1, 1024 * 1024))
    pointer = mupdf.python_mutable_buffer_data(buffer)
    size = 0
    with staged.open("wb") as target:
        while size <= limit:
            count = mupdf.fz_read(source, pointer, min(len(buffer), limit - size + 1))
            if not count:
                return size
            size += count
            if size > limit:
                return None
            target.write(memoryview(buffer)[:count])
    return None


def _pdf_update_attachment_stream(pymupdf, document, pdf, stream, payload: bytes):
    mupdf = pymupdf.mupdf
    original = mupdf.pdf_to_num(stream)
    xref = document.get_new_xref()
    document.update_object(xref, document.xref_object(original, compressed=True))
    document.update_stream(xref, payload, compress=True)
    modified = mupdf.pdf_load_object(pdf, xref)
    params = mupdf.pdf_dict_gets(modified, "Params")
    params = mupdf.pdf_copy_dict(params) if mupdf.pdf_is_dict(params) else mupdf.pdf_new_dict(pdf, 2)
    mupdf.pdf_dict_puts(params, "Size", mupdf.pdf_new_int(len(payload)))
    mupdf.pdf_dict_puts(modified, "Params", params)
    if not mupdf.pdf_is_null(mupdf.pdf_dict_gets(modified, "DL")):
        document.xref_set_key(xref, "DL", str(len(payload)))
    if not mupdf.pdf_is_null(mupdf.pdf_dict_gets(params, "CheckSum")):
        checksum = hashlib.md5(payload, usedforsecurity=False).hexdigest()
        document.xref_set_key(xref, "Params/CheckSum", f"<{checksum}>")
    return mupdf.pdf_new_indirect(pdf, xref, 0)


def _pdf_transform_attachment_payloads(pymupdf, document, pdf, streams, scratch: Path, transform: AttachmentTransform) -> bool:
    mupdf = pymupdf.mupdf
    changed = False
    bytes_read = 0
    bytes_written = 0
    if not streams:
        return False
    with tempfile.TemporaryDirectory(prefix=".machina-pdf-", dir=scratch) as area:
        directory = Path(area)
        for entries in streams.values():
            stream = entries[0][0]
            names = [name for _, _, _, name, protected in entries if name and not protected]
            if not names or any(protected for _, _, _, _, protected in entries):
                continue
            suffixes = {"".join(PurePosixPath(name).suffixes).casefold() for name in names}
            if len(suffixes) != 1:
                continue
            suffix = suffixes.pop()
            if not re.fullmatch(r"[.a-zA-Z0-9_-]{1,80}", suffix):
                continue
            if mupdf.pdf_to_name(mupdf.pdf_dict_gets(stream, "Type")) not in {"", "EmbeddedFile"}:
                continue
            if not mupdf.pdf_is_null(mupdf.pdf_dict_gets(stream, "F")):
                continue  # The stream may be backed by an external file.
            limit = min(_PDF_ATTACHMENT_LIMIT, _PDF_ATTACHMENT_TOTAL - bytes_read)
            output_limit = min(_PDF_ATTACHMENT_LIMIT, _PDF_ATTACHMENT_TOTAL - bytes_written)
            if min(limit, output_limit) <= 0:
                break
            source = directory / ("attachment" + suffix)
            destination = directory / ("rewritten" + suffix)
            size = _pdf_write_attachment_payload(mupdf, stream, source, limit)
            bytes_read += size if size is not None else limit
            if size is None:
                continue
            destination.unlink(missing_ok=True)
            transformed = transform(names[0], source, destination)
            if not isinstance(transformed, bool):
                raise ValueError("PDF attachment transform must return a Boolean")
            if not transformed:
                continue
            if destination.is_symlink() or not destination.is_file():
                raise ValueError("PDF attachment transform did not write a regular file")
            with destination.open("rb") as output:
                payload = output.read(output_limit + 1)
            if len(payload) > output_limit:
                raise ValueError("PDF attachment output exceeds the supported size")
            if payload == source.read_bytes():
                continue
            reference = _pdf_update_attachment_stream(pymupdf, document, pdf, stream, payload)
            for _, files, key, _, _ in entries:
                mupdf.pdf_dict_puts(files, key, reference)
            bytes_written += len(payload)
            changed = True
    return changed


def _pdf_rewrite_attachments(pymupdf, document, replace: Replacement, scratch: Path, transform: AttachmentTransform | None) -> bool:
    mupdf = pymupdf.mupdf
    pdf = pymupdf._as_pdf_document(document)
    catalog = mupdf.pdf_new_indirect(pdf, document.pdf_catalog(), 0)
    tree = mupdf.pdf_dict_getp(catalog, "Names/EmbeddedFiles")
    lock_names = not mupdf.pdf_is_null(mupdf.pdf_dict_gets(catalog, "Collection"))
    specs = []
    streams = defaultdict(list)
    for obj in _pdf_objects(mupdf, catalog):
        action = mupdf.pdf_to_name(mupdf.pdf_dict_gets(obj, "S"))
        uri = _pdf_string(mupdf, obj, "URI")
        lock_names |= action in {"GoToE", "JavaScript"} or "#ef=" in uri.casefold()
        files = mupdf.pdf_dict_gets(obj, "EF")
        if not mupdf.pdf_is_dict(files):
            continue
        spec_streams = []
        protected = mupdf.pdf_to_name(mupdf.pdf_dict_gets(obj, "AFRelationship")) in _PDF_PROTECTED_ATTACHMENTS
        protected |= not mupdf.pdf_is_null(mupdf.pdf_dict_gets(obj, "RF"))
        fallback = next((value for key in _PDF_FILE_NAMES if (value := _pdf_string(mupdf, obj, key))), "")
        for key in _PDF_FILE_NAMES:
            stream = mupdf.pdf_dict_gets(files, key)
            if mupdf.pdf_to_num(stream) and mupdf.pdf_is_stream(stream):
                name = _attachment_basename(_pdf_string(mupdf, obj, key) or fallback)
                spec_streams.append((stream, files, key, name, protected))
        if spec_streams:
            specs.append((obj, protected))
            lock_names |= protected
            for entry in spec_streams:
                streams[mupdf.pdf_to_num(entry[0])].append(entry)

    changed = False
    if specs and not lock_names:
        name_updates = []
        original_names = defaultdict(set)
        for spec, _ in specs:
            for key in _PDF_FILE_NAMES:
                before = _pdf_string(mupdf, spec, key)
                after = _renamed_attachment(before, replace)
                if before:
                    original_names[after.casefold()].add(before.casefold())
                if before != after:
                    name_updates.append((spec, key, after))
        lock_names = any(len(names) > 1 for names in original_names.values())
        if not lock_names and not mupdf.pdf_is_null(tree):
            try:
                changed |= _pdf_rename_attachment_keys(mupdf, tree, replace)
            except ValueError:
                lock_names = True  # Keep file specs and name-tree keys in sync.
        if not lock_names:
            for spec, key, value in name_updates:
                mupdf.pdf_dict_puts(spec, key, mupdf.pdf_new_text_string(value))
                changed = True

    for spec, protected in specs:
        if not protected:
            before = _pdf_string(mupdf, spec, "Desc")
            after = replace(before) if before else before
            if before != after:
                mupdf.pdf_dict_puts(spec, "Desc", mupdf.pdf_new_text_string(after))
                changed = True
    if transform is not None:
        changed |= _pdf_transform_attachment_payloads(pymupdf, document, pdf, streams, scratch, transform)
    return changed


def _rewrite_pdf(
    source: Path, destination: Path, replace: Replacement, attachment_transform: AttachmentTransform | None = None
) -> bool:
    try:
        import pymupdf
    except ImportError as error:
        raise RuntimeError("Install the PDF extra to enable PDF rewriting") from error

    with pymupdf.open(source) as document:
        if not document.is_pdf or document.needs_pass:
            raise ValueError("PDF is invalid or password protected")
        metadata = document.metadata
        updated = {
            key: replace(value) if key in _PDF_META_FIELDS and value else value
            for key, value in metadata.items()
        }
        changed = updated != metadata
        if changed:
            document.set_metadata(updated)

        xml = document.get_xml_metadata()
        if xml:
            updated_xml = _pdf_xmp(xml, replace)
            if updated_xml != xml:
                document.set_xml_metadata(updated_xml)
                changed = True

        changed |= _pdf_rewrite_attachments(pymupdf, document, replace, destination.parent, attachment_transform)

        for page in document:
            changed |= _pdf_rewrite_page(pymupdf, page, replace)
            for link in page.get_links():
                updated_link = dict(link)
                for key in {"uri", "file"} & link.keys():
                    updated_link[key] = replace(link[key])
                if updated_link != link:
                    page.update_link(updated_link)
                    changed = True
            for annotation in page.annots():
                info = annotation.info
                updated_info = {key: replace(info[key]) for key in _PDF_ANNOT_FIELDS if info.get(key)}
                if any(info[key] != value for key, value in updated_info.items()):
                    annotation.set_info(**updated_info)
                    if annotation.type[0] == pymupdf.PDF_ANNOT_FREE_TEXT:
                        annotation.update()
                    changed = True

        if changed:
            if document.get_sigflags() > 0:
                raise ValueError("Signed PDF requires re-signing after modification")
            document.save(destination, garbage=4, deflate=True)
        return changed
