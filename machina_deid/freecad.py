"""Edit typed FreeCAD properties and native TechDraw templates in FCStd packages."""

from __future__ import annotations

import copy
import os
import shutil
import stat
import tempfile
import zipfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from xml.parsers import expat

from .archives import _zip_extra
from .svg import SUPPORTED_SUFFIXES as SVG_SUFFIXES, _patch_attributes, rewrite_svg


SUPPORTED_SUFFIXES = frozenset({".fcstd", ".fcstd1", ".fcbak"})
_DOCUMENT = "Document.xml"
_GUI = "GuiDocument.xml"
_MAX_MEMBER_BYTES = 2_000_000_000
_MAX_PACKAGE_BYTES = 8_000_000_000
_MAX_PACKAGE_MEMBERS = 100_000
_MAX_XML_BYTES = 64 * 1024 * 1024
_MAX_TEMPLATE_BYTES = 96 * 1024 * 1024
_METADATA = {"Comment", "Company", "CreatedBy", "FileName", "Id", "Label", "LastModifiedBy", "LicenseURL"}
_STRING = "App::PropertyString"
_MAP = "App::PropertyMap"
_STRING_LIST = "App::PropertyStringList"
_FILE = "App::PropertyFile"
_FILE_INCLUDED = "App::PropertyFileIncluded"
_TEMPLATE = "TechDraw::DrawSVGTemplate"
_ANNOTATIONS = {"TechDraw::DrawViewAnnotation", "TechDraw::DrawViewSymbol"}
_X_LINK_TYPES = {"App::PropertyXLink", "App::PropertyXLinkList", "App::PropertyXLinkSub", "App::PropertyXLinkSubList"}


def _members(package: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    records = package.infolist()
    if len(records) > _MAX_PACKAGE_MEMBERS:
        raise ValueError("FreeCAD package has too many members")
    members = {}
    folded = set()
    total = 0
    for item in records:
        name = item.orig_filename
        parts = name.removesuffix("/").split("/")
        unsafe = (
            name != item.filename or any(part in {"", ".", ".."} for part in parts)
            or any(character in "\\:" or ord(character) < 32 for character in name)
        )
        directory = item.is_dir()
        unix_type = stat.S_IFMT(item.external_attr >> 16) if item.create_system == 3 else 0
        if unsafe or unix_type not in {0, stat.S_IFDIR if directory else stat.S_IFREG} or item.flag_bits & 1:
            raise ValueError("FreeCAD package has an unsupported member path or type")
        folded_name = name.casefold()
        if folded_name in folded:
            raise ValueError("FreeCAD package contains ambiguous member names")
        folded.add(folded_name)
        total += item.file_size
        if item.file_size > _MAX_MEMBER_BYTES or total > _MAX_PACKAGE_BYTES or (directory and item.file_size):
            raise ValueError("FreeCAD package exceeds its expansion limit")
        members[name] = item
    if _DOCUMENT not in members:
        raise ValueError("FreeCAD package has no root Document.xml")
    return members


def _parse_xml(data: bytes, begin=None, end=None) -> None:
    parser = expat.ParserCreate(encoding="UTF-8")
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)

    def reject(*_args):
        raise ValueError("FreeCAD metadata uses an unsupported XML declaration")

    def check_encoding(_version, encoding, _standalone):
        if encoding and encoding.casefold() not in {"utf-8", "utf8", "ascii", "us-ascii"}:
            reject()

    def start_element(tag, attributes):
        nonlocal root
        if root is None:
            root = tag
            if root != "Document":
                raise ValueError("FreeCAD metadata has an invalid root element")
        if begin is not None:
            begin(tag, attributes, parser.CurrentByteIndex)

    root = None
    parser.StartElementHandler = start_element
    parser.EndElementHandler = end
    parser.StartDoctypeDeclHandler = reject
    parser.EntityDeclHandler = reject
    parser.SkippedEntityHandler = reject
    parser.ExternalEntityRefHandler = lambda *_args: 0
    parser.XmlDeclHandler = check_encoding
    try:
        parser.Parse(data, True)
    except expat.ExpatError as error:
        raise ValueError("FreeCAD metadata is malformed XML") from error


def _rewrite_metadata(
    data: bytes, replace: Callable[[str], str], member_names: dict[str, zipfile.ZipInfo], *, gui: bool = False
) -> tuple[bytes, set[str]]:
    tags: list[str] = []
    objects: dict[str, str] = {}
    templates: set[str] = set()
    attachments: set[str] = set()
    label_references: list[str] = []
    document_label: tuple[int, str] | None = None
    patches: list[tuple[int, int, bytes]] = []
    object_type = ""
    property_name = ""
    property_type = ""

    def rewrite_attr(offset: int, name: str, before: str) -> None:
        if before:
            after = replace(before)
            if before != after:
                _patch_attributes(data, offset, {name.encode("ascii"): after}, patches)

    def begin(tag: str, attrs: dict[str, str], offset: int) -> None:
        nonlocal property_name, property_type, object_type, document_label
        tags.append(tag)
        if tags == ["Document", "Objects", "Object"]:
            if attrs.get("name") and attrs.get("type"):
                objects[attrs["name"]] = attrs["type"]
        elif tags == ["Document", "ObjectData", "Object"]:
            object_type = objects.get(attrs.get("name", ""), "")
        elif len(tags) >= 3 and tags[-2:] == ["Properties", "Property"]:
            property_name = attrs.get("name", "")
            property_type = attrs.get("type", "")
        elif property_type:
            top_level = tags[:3] == ["Document", "Properties", "Property"]
            object_property = not gui and tags[:5] == ["Document", "ObjectData", "Object", "Properties", "Property"]
            if top_level and property_type == _STRING and tag == "String" and len(tags) == 4:
                value = attrs.get("value", "")
                if property_name == "Label":
                    document_label = offset, value
                elif property_name in _METADATA:
                    rewrite_attr(offset, "value", value)
            elif top_level and property_name == "Meta" and property_type == _MAP and tags[3:] == ["Map", "Item"]:
                rewrite_attr(offset, "value", attrs.get("value", ""))
            elif object_property:
                field = tags[5:]
                if property_name == "Label2" and property_type == _STRING and field == ["String"]:
                    rewrite_attr(offset, "value", attrs.get("value", ""))
                if tag == "XLink" and property_type in _X_LINK_TYPES:
                    value = attrs.get("file", "")
                    if value not in member_names and PurePosixPath(value.replace("\\", "/")).suffix.casefold() in SUPPORTED_SUFFIXES:
                        rewrite_attr(offset, "file", value)
                if object_type == _TEMPLATE:
                    if property_name == "EditableTexts" and property_type == _MAP and field == ["Map", "Item"]:
                        rewrite_attr(offset, "value", attrs.get("value", ""))
                    if property_name == "Template" and property_type == _FILE and field == ["File"]:
                        value = attrs.get("value", "")
                        if value not in member_names and PurePosixPath(value.replace("\\", "/")).suffix.casefold() in SVG_SUFFIXES:
                            rewrite_attr(offset, "value", value)
                if object_type in _ANNOTATIONS and property_name in {"Text", "EditableTexts"}:
                    if property_type == _STRING_LIST and field == ["StringList", "String"]:
                        rewrite_attr(offset, "value", attrs.get("value", ""))
                if property_type == _FILE_INCLUDED and field == ["FileIncluded"]:
                    target = attrs.get("file", "")
                    if object_type == _TEMPLATE and property_name == "PageResult":
                        if target in member_names and PurePosixPath(target).suffix.casefold() in SVG_SUFFIXES:
                            templates.add(target)
                    else:
                        attachments.add(target)
            if property_type == "App::PropertyExpressionEngine" and tag == "Expression":
                label_references.extend(attrs.values())
            if property_type.startswith(("App::PropertyLink", "App::PropertyXLink")):
                label_references.extend(attrs.values())

    def end(tag: str) -> None:
        nonlocal property_name, property_type, object_type
        if tag == "Property" and tags[-2:] == ["Properties", "Property"]:
            property_name = ""
            property_type = ""
        if tags == ["Document", "ObjectData", "Object"]:
            object_type = ""
        tags.pop()

    _parse_xml(data, begin, end)
    if document_label is not None:
        offset, value = document_label
        if value and not any(value in reference for reference in label_references):
            rewrite_attr(offset, "value", value)
    templates.difference_update(attachments)
    if not patches:
        return data, templates

    output = []
    last = 0
    for start, end, updated in sorted(patches):
        output.extend((data[last:start], updated))
        last = end
    output.append(data[last:])
    rewritten = b"".join(output)
    if len(rewritten) > _MAX_XML_BYTES:
        raise ValueError("FreeCAD metadata exceeds its supported size")
    _parse_xml(rewritten)
    return rewritten, templates


def _rewrite_templates(
    package: zipfile.ZipFile, members: dict[str, zipfile.ZipInfo], names: set[str],
    destination: Path, replace: Callable[[str], str],
) -> dict[str, bytes]:
    rewritten = {}
    if not names:
        return rewritten
    with tempfile.TemporaryDirectory(prefix=".machina-freecad-svg-", dir=destination.parent) as directory:
        for name in sorted(names):
            info = members[name]
            if info.file_size > _MAX_TEMPLATE_BYTES:
                raise ValueError("FreeCAD TechDraw template exceeds its supported size")
            suffix = PurePosixPath(name).suffix.casefold()
            source = Path(directory, "source" + suffix)
            target = Path(directory, "rewritten" + suffix)
            source.write_bytes(package.read(info))
            if rewrite_svg(source, target, replace):
                rewritten[name] = target.read_bytes()
    return rewritten


def _write_package(package: zipfile.ZipFile, destination: Path, updates: dict[str, bytes]) -> None:
    descriptor, filename = tempfile.mkstemp(prefix=".machina-freecad-", dir=destination.parent)
    os.close(descriptor)
    staged = Path(filename)
    try:
        with zipfile.ZipFile(staged, "w") as output:
            output.comment = package.comment
            for item in package.infolist():
                updated = updates.get(item.filename)
                member = copy.copy(item)
                member.extra = _zip_extra(item.extra, renamed=False)
                if updated is not None:
                    output.writestr(member, updated)
                else:
                    with package.open(item) as reader, output.open(member, "w") as writer:
                        shutil.copyfileobj(reader, writer, length=1024 * 1024)
        os.replace(staged, destination)
    finally:
        staged.unlink(missing_ok=True)


def rewrite_freecad(source: Path, destination: Path, replace: Callable[[str], str]) -> bool:
    """Write changed native data and preserve opaque package members as bytes."""
    if source.suffix.casefold() not in SUPPORTED_SUFFIXES:
        return False
    if source.resolve() == destination.resolve():
        raise ValueError("FreeCAD source and destination must differ")
    try:
        with zipfile.ZipFile(source) as package:
            members = _members(package)
            updates = {}
            templates = set()
            for name in (_DOCUMENT, _GUI):
                info = members.get(name)
                if info is None:
                    continue
                if info.file_size > _MAX_XML_BYTES:
                    raise ValueError("FreeCAD metadata exceeds its supported size")
                original = package.read(info)
                rewritten, attached = _rewrite_metadata(original, replace, members, gui=name == _GUI)
                templates.update(attached)
                if rewritten is not original:
                    updates[name] = rewritten
            updates.update(_rewrite_templates(package, members, templates, destination, replace))
            if updates:
                _write_package(package, destination, updates)
            return bool(updates)
    except (RuntimeError, NotImplementedError, zipfile.BadZipFile, zipfile.LargeZipFile) as error:
        raise ValueError("FreeCAD package could not be read or written") from error
