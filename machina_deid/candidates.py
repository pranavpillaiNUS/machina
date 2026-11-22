"""Discover reviewable identity candidates without printing source values."""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from collections.abc import Iterable, Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from itertools import islice
from pathlib import Path
from tempfile import TemporaryDirectory

from .corpus import filesystem_path, scan
from .discovery import classify_suffix
from .dxf import dxf_encoding, header_values, read_records
from .formats import _step_decode
from .oda import DWG_VERSIONS, convert, converter_command, dwg_version


STEP_HEADER_BYTES = 64 * 1024
_STEP_SUFFIXES = {".p21", ".step", ".stp"}
_STEP_TOKENS = re.compile(
    rb"(?P<space>\s+)|(?P<comment>/\*.*?\*/)|'(?:[^']|'')*'|[A-Za-z_!][A-Za-z0-9_!-]*|.",
    re.DOTALL,
)

_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_HOST = rf"(?:{_LABEL}\.)+(?:[A-Za-z]{{2,63}}|xn--[A-Za-z0-9-]{{2,59}})"
_EMAIL_ATOM = r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~+\-]+"
_HOST_END = r"(?![A-Za-z0-9_-]|\.[A-Za-z0-9_-])"
_EMAIL = re.compile(
    rf"(?<![\w.!#$%&'*+/=?^`{{|}}~+\-@]){_EMAIL_ATOM}(?:\.{_EMAIL_ATOM})*"
    rf"@(?P<domain>{_HOST}){_HOST_END}"
)
_URL_HOST = re.compile(rf"(?i)\b(?:https?://|(?=www\.))(?P<domain>{_HOST}){_HOST_END}")
_BARE_HOST = re.compile(rf"{_HOST}\Z")
_DISPLAY_NAME = re.compile(r"([^<>]+)\s*<([^<>]+)>\Z")
_PLACEHOLDERS = frozenset({
    "-", "?", "admin", "administrator", "anonymous", "author", "company", "default", "drawing",
    "draft", "model", "n/a", "na", "none", "null", "organization", "part", "sample", "template",
    "unknown", "unspecified", "untitled", "user",
})


class _HeaderError(ValueError):
    """An expected STEP header field could not be recognized."""


@dataclass
class _Evidence:
    files: int = 0
    fields: Counter[str] = field(default_factory=Counter)
    spellings: Counter[str] = field(default_factory=Counter, repr=False)

    def review_record(self, kind: str) -> dict[str, object]:
        values = sorted(self.spellings, key=lambda value: (-self.spellings[value], value))
        return {
            "kind": kind,
            "source": values[0],
            "aliases": values[1:],
            "files": self.files,
            "fields": dict(sorted(self.fields.items())),
        }


def discover_candidates(input_root: Path, private_output: Path) -> dict[str, object]:
    """Write a new private review list and return only aggregate counters."""
    root = filesystem_path(input_root.resolve())
    output = filesystem_path(private_output.resolve())
    if output.is_relative_to(root):
        raise ValueError("Keep the candidate review file outside the input corpus.")
    if output.exists():
        raise ValueError("The candidate review file already exists.")

    evidence: dict[tuple[str, str], _Evidence] = {}
    inspected: Counter[str] = Counter()
    skipped: Counter[str] = Counter()
    pdf_module = _pdf_module()
    converter = converter_command()
    messages = _quiet_pdf_messages(pdf_module) if pdf_module else nullcontext()

    with messages:
        for entry in scan(root):
            suffix = entry.relative.suffix.lower()
            if suffix not in _STEP_SUFFIXES and suffix not in {".pdf", ".dxf", ".dwg"}:
                continue
            source = root.joinpath(*entry.relative.parts)
            if suffix in {".dxf", ".dwg"}:
                if suffix == ".dwg" and converter is None:
                    skipped["dwg_converter_unavailable"] += 1
                    continue
                try:
                    metadata = _cad_metadata(source, suffix == ".dwg")
                except (OSError, ValueError):
                    skipped[suffix[1:] + "_header_unavailable"] += 1
                    continue
                inspected[suffix[1:]] += 1
            elif suffix in _STEP_SUFFIXES:
                try:
                    metadata = _step_metadata(source)
                except (OSError, _HeaderError):
                    skipped["step_header_unavailable"] += 1
                    continue
                inspected["step"] += 1
            else:
                if pdf_module is None:
                    skipped["pdf_adapter_unavailable"] += 1
                    continue
                try:
                    metadata = _pdf_metadata(source, pdf_module)
                except (OSError, RuntimeError, ValueError):
                    skipped["pdf_info_unavailable"] += 1
                    continue
                inspected["pdf"] += 1
            _add_evidence(evidence, metadata)

    ordered = sorted(evidence, key=lambda key: (key[0], -evidence[key].files, key[1]))
    summary = {
        "candidate_count": len(evidence),
        "by_kind": dict(sorted(Counter(kind for kind, _ in evidence).items())),
        "inspected": dict(sorted(inspected.items())),
        "skipped": dict(sorted(skipped.items())),
    }
    candidates = [evidence[key].review_record(key[0]) for key in ordered]
    _write_private_review(output, {"version": 1, "summary": summary, "candidates": candidates})
    return summary


def _write_private_review(output: Path, report: dict[str, object]) -> None:
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as review:
            json.dump(report, review, ensure_ascii=False, indent=2)
            review.write("\n")
    except BaseException:
        output.unlink(missing_ok=True)
        raise


def _take(tokens: Iterator[bytes], expected: bytes) -> None:
    if next(tokens, b"").upper() != expected:
        raise _HeaderError("Unrecognized STEP header structure.")


def _skip_step_record(tokens: Iterator[bytes]) -> None:
    _take(tokens, b"(")
    depth = 1
    for token in tokens:
        if token == b"(":
            depth += 1
        elif token == b")":
            depth -= 1
            if depth == 0:
                _take(tokens, b";")
                return
        elif token == b";":
            break
    raise _HeaderError("Incomplete STEP header record.")


def _step_values(tokens: Iterator[bytes]) -> list[str]:
    _take(tokens, b"(")
    token = next(tokens, b"")
    result = []
    if token == b")":
        return result
    while len(token) >= 2 and token[:1] == token[-1:] == b"'":
        try:
            result.append(_step_decode(token[1:-1]))
        except ValueError:
            # The syntax is a string, but this exporter's character directive is unsupported.
            pass
        separator = next(tokens, b"")
        if separator == b")":
            return result
        if separator != b",":
            break
        token = next(tokens, b"")
    raise _HeaderError("Unrecognized STEP author or organization list.")


def _step_text(tokens: Iterator[bytes]) -> None:
    token = next(tokens, b"")
    if token not in (b"$", b"*") and (len(token) < 2 or token[:1] != b"'" or token[-1:] != b"'"):
        raise _HeaderError("Unrecognized STEP FILE_NAME string.")


def _file_name_metadata(tokens: Iterator[bytes]) -> list[tuple[str, str, str]]:
    _take(tokens, b"(")
    _step_text(tokens)  # filename
    _take(tokens, b",")
    _step_text(tokens)  # timestamp
    _take(tokens, b",")
    authors = _step_values(tokens)
    _take(tokens, b",")
    organizations = _step_values(tokens)
    for _ in range(3):  # preprocessor, originating system, authorization
        _take(tokens, b",")
        _step_text(tokens)
    _take(tokens, b")")
    _take(tokens, b";")
    return [("author", "step.author", value) for value in authors] + [
        ("organization", "step.organization", value) for value in organizations
    ]


def _step_metadata(source: Path) -> list[tuple[str, str, str]]:
    with source.open("rb") as handle:
        prefix = handle.read(STEP_HEADER_BYTES)
    tokens = (match[0] for match in _STEP_TOKENS.finditer(prefix.removeprefix(b"\xef\xbb\xbf")) if not match.lastgroup)
    if tuple(token.upper() for token in islice(tokens, 4)) != (b"ISO-10303-21", b";", b"HEADER", b";"):
        raise _HeaderError("Missing STEP exchange header.")
    for token in tokens:
        keyword = token.upper()
        if keyword == b"FILE_NAME":
            return _file_name_metadata(tokens)
        if keyword in (b"ENDSEC", b"DATA"):
            break
        _skip_step_record(tokens)
    raise _HeaderError("STEP FILE_NAME not present within the header read limit.")


def _pdf_module():
    try:
        import pymupdf
    except ImportError:
        return None
    return pymupdf


@contextmanager
def _quiet_pdf_messages(module):
    tools = module.TOOLS
    errors = tools.mupdf_display_errors()
    warnings = tools.mupdf_display_warnings()
    tools.mupdf_display_errors(False)
    tools.mupdf_display_warnings(False)
    try:
        yield
    finally:
        tools.mupdf_display_errors(errors)
        tools.mupdf_display_warnings(warnings)


def _pdf_metadata(source: Path, module) -> list[tuple[str, str, str]]:
    try:
        with module.open(source) as document:
            metadata = document.metadata
            if not document.is_pdf or metadata is None:
                raise ValueError("PDF document Info is not accessible.")
            return [(kind, f"pdf.{kind}", metadata.get(kind) or "") for kind in ("author", "title")]
    finally:
        # MuPDF retains diagnostics even with output disabled. Keep bulk scans bounded.
        module.TOOLS.reset_mupdf_warnings()


_CAD_FIELDS = {
    "$AUTHOR": "author", "$LASTSAVEDBY": "author", "$TITLE": "title",
    "$PROJECTNAME": "project", "$CUSTOMPROPERTY": "custom",
}


def _cad_metadata(source: Path, dwg: bool) -> list[tuple[str, str, str]]:
    if dwg:
        version = dwg_version(source)
        with TemporaryDirectory(prefix="machina-candidates-") as folder:
            data = convert(source, Path(folder), DWG_VERSIONS[version], "DXF").read_bytes()
    else:
        data = source.read_bytes()
    records = read_records(data)
    encoding = dxf_encoding(records)
    metadata = []
    for name, value in header_values(records):
        # ODA stamps its own account into $LASTSAVEDBY on export, so a DWG's value is not evidence.
        if name in _CAD_FIELDS and not (dwg and name == "$LASTSAVEDBY"):
            metadata.append((_CAD_FIELDS[name], "dxf." + name, value.decode(encoding, errors="replace")))
    return metadata


def _add_evidence(evidence: dict[tuple[str, str], _Evidence], metadata: Iterable[tuple[str, str, str]]) -> None:
    per_file: dict[tuple[str, str], tuple[set[str], set[str]]] = {}
    for kind, origin, value in metadata:
        for type_name, candidate in _field_candidates(kind, value):
            spellings, fields = per_file.setdefault((type_name, candidate.casefold()), (set(), set()))
            spellings.add(candidate)
            fields.add(origin)

    for key, (spellings, fields) in per_file.items():
        entry = evidence.setdefault(key, _Evidence())
        entry.files += 1
        entry.fields.update(fields)
        entry.spellings.update(spellings)


def _field_candidates(kind: str, value: str) -> Iterator[tuple[str, str]]:
    value = value.strip()
    if not value or len(value) > 4096:
        return
    emails = list(_EMAIL.finditer(value))
    for match in emails:
        if len(match[0]) <= 254:
            yield "email", match[0]
            yield "domain", match["domain"]
    urls = list(_URL_HOST.finditer(value))
    for match in urls:
        yield "domain", match["domain"]
    if not emails and not urls and kind != "title" and _BARE_HOST.fullmatch(value) and classify_suffix(value) == "(other)":
        yield "domain", value
        return

    if emails or urls:
        display = _DISPLAY_NAME.fullmatch(value)
        if kind != "author" or display is None or _EMAIL.fullmatch(display[2]) is None:
            return
        value = display[1].strip().strip('"').strip()
    if (
        2 <= len(value) <= 180 and value.isprintable() and value.casefold() not in _PLACEHOLDERS
        and any(char.isalpha() for char in value)
    ):
        yield kind, value
