"""Apply explicit identity rules and keep references aligned with renamed files."""

from __future__ import annotations

import hashlib
import hmac
import json
import posixpath
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .discovery import COMPOUND_SUFFIXES


_WORD_CHARACTER = r"[^\W_]"
_CATEGORY = re.compile(r"[A-Za-z][A-Za-z0-9-]{0,31}\Z")
_REFERENCE = re.compile(
    r'''"(?P<double>[^"\r\n]{1,4096})"|'''
    r"'(?P<single>[^'\r\n]{1,4096})'|"
    r'''(?P<bare>[^\s"'<>|()[\]{},;=]{1,4096})'''
)
_SEPARATORS = re.compile(r"([/\\])")


@dataclass(frozen=True)
class _Rule:
    source: str
    replacement: str
    scope: str
    match: str
    ignore_case: bool

    def expression(self) -> str:
        expression = re.escape(self.source)
        if self.ignore_case:
            expression = f"(?i:{expression})"
        if self.match == "token":
            expression = f"(?<!{_WORD_CHARACTER}){expression}(?!{_WORD_CHARACTER})"
        return expression


class _Matcher:
    def __init__(self, rules: Iterable[_Rule]):
        prioritized = sorted(rules, key=lambda rule: (-len(rule.source), rule.ignore_case, rule.source))
        self._replacements = {
            f"rule_{index}": rule.replacement for index, rule in enumerate(prioritized)
        }
        expressions = [
            f"(?P<rule_{index}>{rule.expression()})" for index, rule in enumerate(prioritized)
        ]
        self._pattern = re.compile("|".join(expressions)) if expressions else None

    def replace(self, text: str) -> str:
        if self._pattern is None:
            return text
        return self._pattern.sub(lambda match: self._replacements[match.lastgroup], text)


class Rulebook:
    """An explicit, local list of identities and their pseudonyms."""

    def __init__(self, rules: Iterable[_Rule] = ()):
        rules = tuple(rules)
        self._content = _Matcher(rule for rule in rules if rule.scope != "path")
        self._path = _Matcher(rule for rule in rules if rule.scope != "content")

    @classmethod
    def from_file(cls, path: str | Path, *, key: str | bytes | None = None) -> Rulebook:
        with Path(path).open(encoding="utf-8-sig") as rule_file:
            return cls.from_dict(json.load(rule_file), key=key)

    @classmethod
    def from_dict(cls, data: Mapping[str, object], *, key: str | bytes | None = None) -> Rulebook:
        if not isinstance(data, Mapping) or set(data) != {"rules"} or not isinstance(data["rules"], list):
            raise ValueError("Rulebook must be an object containing a rules list")
        if isinstance(key, str):
            key = key.encode("utf-8")
        rules = []
        sources: dict[str, list[tuple[str, str, bool, str]]] = {}
        identities: dict[str, str] = {}
        pseudonyms: dict[str, str] = {}
        for index, entry in enumerate(data["rules"], 1):
            for rule, identity in _parse_entry(entry, index, key):
                known_replacement = identities.setdefault(identity, rule.replacement)
                if known_replacement != rule.replacement:
                    raise ValueError(f"Rule {index} assigns multiple replacements to the same identity")
                known_identity = pseudonyms.setdefault(rule.replacement.casefold(), identity)
                if known_identity != identity:
                    raise ValueError(f"Rule {index} gives distinct identities the same replacement")
                matches = sources.setdefault(rule.source.casefold(), [])
                for source, scope, ignore_case, owner in matches:
                    if owner == identity:
                        continue
                    scopes_overlap = scope == rule.scope or "both" in (scope, rule.scope)
                    if scopes_overlap and (source == rule.source or ignore_case or rule.ignore_case):
                        raise ValueError(f"Rule {index} assigns one matching value to distinct identities")
                matches.append((rule.source, rule.scope, rule.ignore_case, identity))
                rules.append(rule)
        return cls(dict.fromkeys(rules))

    def replace(self, text: str) -> str:
        return self._content.replace(text)

    def _component(self, name: str, *, file: bool = False) -> str:
        suffix = ""
        if file:
            suffix = PurePosixPath(name).suffix
            for compound in COMPOUND_SUFFIXES:
                if name.casefold().endswith(compound):
                    suffix = name[-len(compound):]
                    break
        stem = name[:-len(suffix)] if suffix else name
        transformed = self._path.replace(stem) + suffix
        if transformed in ("", ".", "..") or any(char in transformed for char in "/\\\0"):
            raise ValueError("Identity rules produced an invalid path component")
        return transformed

    def map_path(self, path: str | PurePosixPath) -> PurePosixPath:
        parts = _relative_path(path).split("/")
        directories = [self._component(part) for part in parts[:-1]]
        return PurePosixPath(*directories, self._component(parts[-1], file=True))


def _parse_entry(entry: object, index: int, key: bytes | None) -> Iterable[tuple[_Rule, str]]:
    allowed_fields = {"source", "replacement", "aliases", "id", "category", "scope", "match", "ignore_case"}
    if not isinstance(entry, Mapping) or set(entry) - allowed_fields:
        raise ValueError(f"Rule {index} contains invalid fields")

    source = entry.get("source")
    aliases = entry.get("aliases", [])
    ignore_case = entry.get("ignore_case", False)
    scope = entry.get("scope", "both")
    match = entry.get("match", "token")
    if not _valid_text(source) or not isinstance(aliases, list) or not all(map(_valid_text, aliases)):
        raise ValueError(f"Rule {index} requires a nonempty source and valid aliases")
    if not isinstance(ignore_case, bool) or scope not in ("both", "content", "path"):
        raise ValueError(f"Rule {index} has invalid ignore_case or scope")
    if match not in ("token", "literal"):
        raise ValueError(f"Rule {index} has an invalid match mode")

    identity = entry.get("id", source.casefold() if ignore_case else source)
    if not _valid_text(identity):
        raise ValueError(f"Rule {index} has an invalid identity ID")
    replacement = entry.get("replacement")
    if replacement is None:
        if not key:
            raise ValueError(f"Rule {index} requires a replacement or a private key")
        category = entry.get("category", "Identity")
        if not isinstance(category, str) or not _CATEGORY.fullmatch(category):
            raise ValueError(f"Rule {index} has an invalid pseudonym category")
        digest = hmac.new(key, identity.encode("utf-8"), hashlib.sha256).hexdigest()[:16]
        replacement = f"{category}-{digest}"
    if not _valid_text(replacement):
        raise ValueError(f"Rule {index} requires a nonempty replacement")

    for alias in (source, *aliases):
        if alias.casefold() == replacement.casefold():
            raise ValueError(f"Rule {index} keeps the original identifier as its replacement")
        yield _Rule(alias, replacement, scope, match, ignore_case), identity


def _valid_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and "\0" not in value


def _relative_path(path: str | PurePosixPath) -> str:
    value = path.as_posix() if isinstance(path, PurePosixPath) else str(path)
    if any(part in ("", ".", "..") for part in value.split("/")) or "\\" in value or "\0" in value:
        raise ValueError("Expected a normalized, slash-separated path relative to the corpus")
    return value


class PathMap:
    """The original and mapped namespaces for one corpus or archive."""

    def __init__(
        self,
        paths: Iterable[str | PurePosixPath],
        rulebook: Rulebook,
        *,
        rename_paths: bool = True,
    ):
        self._rulebook = rulebook
        path_rules = rulebook if rename_paths else Rulebook()
        self._paths: dict[str, str] = {}
        self._directories: dict[str, str] = {"": ""}
        destinations: dict[str, tuple[str, bool]] = {}

        def register(source: str, destination: str, directory: bool) -> None:
            previous = destinations.setdefault(destination, (source, directory))
            if previous != (source, directory):
                raise ValueError("Corpus paths or directories collide after applying identity rules")

        def mapped_directory(directory: str) -> str:
            if directory not in self._directories:
                parent, _, name = directory.rpartition("/")
                destination = posixpath.join(mapped_directory(parent), path_rules._component(name))
                register(directory, destination, True)
                self._directories[directory] = destination
            return self._directories[directory]

        for path in paths:
            source = _relative_path(path)
            directory, _, filename = source.rpartition("/")
            destination = posixpath.join(mapped_directory(directory), path_rules._component(filename, file=True))
            register(source, destination, False)
            self._paths[source] = destination

        self._folded_paths: dict[str, str | None] = {}
        self._filenames: dict[str, tuple[str, str] | None] = {}
        for source, destination in self._paths.items():
            _unique(self._folded_paths, source.casefold(), source)
            filename = posixpath.basename(source)
            mapped_name = posixpath.basename(destination)
            _unique(self._filenames, filename.casefold(), (filename, mapped_name))
        for source in self._directories:
            _unique(self._folded_paths, source.casefold(), source)

    @classmethod
    def build(
        cls,
        paths: Iterable[str | PurePosixPath],
        rulebook: Rulebook,
        *,
        rename_paths: bool = True,
    ) -> PathMap:
        return cls(paths, rulebook, rename_paths=rename_paths)

    def __getitem__(self, path: str | PurePosixPath) -> PurePosixPath:
        return PurePosixPath(self._paths[_relative_path(path)])

    def replace(self, text: str, source_path: str | PurePosixPath | None = None) -> str:
        source = _relative_path(source_path) if source_path is not None else ""
        directory = posixpath.dirname(source)
        whole_reference = self._reference(text, directory)
        if whole_reference is not None:
            return whole_reference

        parts = []
        committed = 0
        for match in _REFERENCE.finditer(text):
            group = match.lastgroup
            candidate = match.group(group)
            if group == "bare" and not any(char in candidate for char in ".\\/"):
                continue
            replacement = self._reference(candidate, directory)
            if replacement is not None:
                start, end = match.span(group)
                parts.append(self._rulebook.replace(text[committed:start]))
                parts.append(replacement)
                committed = end
        parts.append(self._rulebook.replace(text[committed:]))
        return "".join(parts)

    def _lookup(self, candidate: str) -> tuple[str, str] | None:
        if candidate in self._paths:
            return candidate, self._paths[candidate]
        if candidate in self._directories:
            return candidate, self._directories[candidate]
        source = self._folded_paths.get(candidate.casefold())
        if source is not None:
            return source, self._paths.get(source, self._directories.get(source, ""))
        return None

    def _reference(self, value: str, directory: str) -> str | None:
        if not value or len(value) > 4096 or any(char in value for char in "\r\n\0"):
            return None
        normalized = value.replace("\\", "/")
        if not normalized or "://" in normalized and not normalized.lower().startswith("file://"):
            return None

        path = posixpath.normpath(normalized)
        absolute = path.startswith("/") or bool(re.match(r"[A-Za-z]:/", path)) or normalized.lower().startswith("file://")
        if not absolute:
            for candidate in (posixpath.normpath(posixpath.join(directory, path)), path):
                target = self._lookup(candidate)
                if target is not None and ("/" in normalized or target[0] in self._paths):
                    return _rename_reference(value, *target)
        else:
            components = path.split("/")
            for start in range(1, len(components) - 1):
                target = self._lookup("/".join(components[start:]))
                if target is not None:
                    return _rename_reference(value, *target, rewrite_prefix=self._rulebook.replace)

        if "/" not in normalized:
            target = self._filenames.get(path.casefold())
            if target is not None:
                return _rename_reference(value, *target)
        return None


def _unique(index: dict, key: str, value: object) -> None:
    previous = index.setdefault(key, value)
    if previous != value:
        index[key] = None


def _rename_reference(value: str, source: str, destination: str, *, rewrite_prefix=None) -> str:
    source_components = source.split("/")
    destination_components = destination.split("/")
    pieces = _SEPARATORS.split(value)
    name_index = len(source_components) - 1
    prefix_end = len(pieces)
    for index in range(len(pieces) - 1, -1, -2):
        part = pieces[index]
        if part in ("", ".", ".."):
            continue
        if name_index < 0 or part.casefold() != source_components[name_index].casefold():
            break
        if source_components[name_index] != destination_components[name_index]:
            pieces[index] = destination_components[name_index]
        prefix_end = index
        name_index -= 1
    if rewrite_prefix is not None:
        return rewrite_prefix("".join(pieces[:prefix_end])) + "".join(pieces[prefix_end:])
    return "".join(pieces)
