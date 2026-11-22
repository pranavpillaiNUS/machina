"""Prepare a local submission from a completed corpus and a tracked Git snapshot."""

from __future__ import annotations

import json
import math
import os
import re
import stat
import subprocess
import tomllib
from collections import Counter
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory

from .corpus import filesystem_path
from .discovery import COMPOUND_SUFFIXES, KNOWN_SUFFIXES


MAX_CORPUS_FILES = 400_000
SCORER_FILE_BYTES = 2_000_000_000
_SNAPSHOT_DIRECTORIES = {"machina_deid", "tests"}
_SNAPSHOT_FILES = {"LICENSE", "README.md", "pyproject.toml"}
_SNAPSHOT_OPTIONAL_FILES = {
    "docs/autocad-probe.md",
    "tools/hidden-xvfb.sh",
    "tools/oda-neutral.sh",
    "tools/oda_probe.py",
}
_HASH = re.compile(rb"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
_CODE_COMPONENT = re.compile(r"[A-Za-z0-9_.-]+\Z")
_PRIVATE_COMPONENTS = {".local", ".venv", "aliases", "credentials", "input", "logs", "output", "private", "raw"}
_STATUSES = {
    "adapter_failed", "empty", "over_limit", "rewritten", "source_pointer", "unchanged", "unsupported",
}
_FORMATS = KNOWN_SUFFIXES | set(COMPOUND_SUFFIXES) | {"(none)", "(other)"}


def prepare_attempt(
    attempt_root: Path, repo_root: Path, run_report: Path, *, revision: str = "HEAD",
) -> dict[str, object]:
    """Validate corpus totals and create code/ and README.txt for local review."""
    _require_real_directory(attempt_root)
    root = filesystem_path(attempt_root.resolve(strict=True))
    corpus = root / "corpus"
    code = root / "code"
    readme = root / "README.txt"
    _require_real_directory(corpus)
    report = _read_report(run_report)

    lock = root / ".machina-prepare.lock"
    try:
        handle = lock.open("xb")
    except FileExistsError:
        raise ValueError("Another process is preparing this attempt.") from None
    try:
        with handle:
            _require_missing(code, readme)
            actual_files, actual_bytes, large_files = _corpus_totals(corpus)
            if (actual_files, actual_bytes) != (report["files"], report["output_bytes"]):
                raise ValueError("Corpus file count or byte total differs from the run report.")

            commit = _commit(repo_root, revision)
            if report.get("revision") and report["revision"] != commit:
                raise ValueError("The run report refers to a different Git revision.")
            entries = _snapshot_entries(repo_root, commit)

            with TemporaryDirectory(prefix=".machina-package-", dir=root) as temporary:
                staged = Path(temporary) / "code"
                staged.mkdir()
                for path, object_id, executable in entries:
                    target = staged.joinpath(*path.parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with target.open("xb") as blob:
                        _git(repo_root, "cat-file", "blob", object_id, output=blob)
                    if executable and os.name == "posix":
                        target.chmod(0o755)

                instructions = _readme(report, commit, large_files, _extras(staged))
                _publish(staged, code, readme, instructions)
    finally:
        lock.unlink(missing_ok=True)

    return {
        "revision": commit,
        "corpus_files": actual_files,
        "corpus_bytes": actual_bytes,
        "files_over_2_gb": large_files,
        "code_files": len(entries),
    }


def _is_reparse(properties: os.stat_result) -> bool:
    return stat.S_ISLNK(properties.st_mode) or bool(
        getattr(properties, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _require_real_directory(path: Path) -> None:
    try:
        properties = path.lstat()
    except FileNotFoundError:
        raise ValueError("The attempt root and its corpus directory must already exist.") from None
    if _is_reparse(properties) or not stat.S_ISDIR(properties.st_mode):
        raise ValueError("The attempt root and corpus must be real directories.")


def _require_missing(*paths: Path) -> None:
    for path in paths:
        try:
            path.lstat()
        except FileNotFoundError:
            continue
        raise ValueError("The attempt already contains code/ or README.txt.")


def _corpus_totals(corpus: Path) -> tuple[int, int, int]:
    directories = [corpus]
    files = total_bytes = large_files = 0
    while directories:
        with os.scandir(directories.pop()) as children:
            for entry in children:
                properties = entry.stat(follow_symlinks=False)
                if _is_reparse(properties):
                    raise ValueError("The corpus contains a symbolic link or reparse point.")
                if stat.S_ISDIR(properties.st_mode):
                    directories.append(Path(entry.path))
                elif stat.S_ISREG(properties.st_mode):
                    files += 1
                    total_bytes += properties.st_size
                    large_files += properties.st_size > SCORER_FILE_BYTES
                    if files > MAX_CORPUS_FILES:
                        raise ValueError("The corpus exceeds the scorer's 400,000-object limit.")
                else:
                    raise ValueError("The corpus contains a nonregular file.")
    if not files:
        raise ValueError("The corpus contains no files.")
    return files, total_bytes, large_files


def _nonnegative(value: object) -> bool:
    return type(value) is int and value >= 0


def _counts(value: object) -> dict[str, int]:
    if not isinstance(value, dict) or any(key not in _STATUSES or not _nonnegative(count) for key, count in value.items()):
        raise ValueError("The aggregate run report contains invalid coverage counts.")
    return value


def _read_report(path: Path) -> dict[str, object]:
    try:
        raw = json.loads(path.read_bytes())
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError("The run report must be a JSON object.") from None
    if not isinstance(raw, dict):
        raise ValueError("The run report must be a JSON object.")
    files, output_bytes, renamed = (raw.get(key) for key in ("files", "output_bytes", "renamed"))
    if not all(map(_nonnegative, (files, output_bytes, renamed))) or renamed > files:
        raise ValueError("The run report contains invalid corpus totals.")
    statuses = _counts(raw.get("by_status"))
    if sum(statuses.values()) != files:
        raise ValueError("The run report's coverage counts differ from its file count.")

    suffixes = raw.get("by_suffix", {})
    if not isinstance(suffixes, dict) or any(suffix not in _FORMATS for suffix in suffixes):
        raise ValueError("The run report contains an invalid output format label.")
    by_suffix = {suffix: _counts(counts) for suffix, counts in suffixes.items()}
    if by_suffix:
        format_totals: Counter[str] = Counter()
        for counts in by_suffix.values():
            format_totals.update(counts)
        if format_totals != Counter(statuses):
            raise ValueError("The run report's format counts differ from its coverage counts.")
    options = raw.get("run_options")
    if options is not None:
        options = _run_options(options, renamed, statuses)
    commit = raw.get("revision")
    if commit is not None and (not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", commit)):
        raise ValueError("The run report contains an invalid Git revision.")
    return {
        "files": files,
        "output_bytes": output_bytes,
        "renamed": renamed,
        "by_status": statuses,
        "archive_member_status": _counts(raw.get("archive_member_status", {})),
        "by_suffix": by_suffix,
        "run_options": options,
        "revision": commit,
    }


def _run_options(options: object, renamed: int, statuses: dict[str, int]) -> dict[str, object]:
    if not isinstance(options, dict):
        raise ValueError("The run report contains invalid run settings.")
    workers = options.get("workers")
    edit_mib = options.get("max_edit_mib")
    rename_paths = options.get("rename_paths")
    preserve_on_error = options.get("preserve_on_error")
    edit_dwg = options.get("edit_dwg", False)
    bound = edit_mib * 1024**2 if type(edit_mib) in {int, float} else 0
    if (
        not _nonnegative(workers) or not 1 <= workers <= 32
        or bound < 1 or bound > (1 << 53) or not math.isfinite(bound) or int(bound) != bound
        or not isinstance(rename_paths, bool) or not isinstance(preserve_on_error, bool)
        or not isinstance(edit_dwg, bool)
        or renamed and not rename_paths or statuses.get("adapter_failed", 0) and not preserve_on_error
    ):
        raise ValueError("The run report contains invalid run settings.")
    return {
        "workers": workers,
        "max_edit_bytes": int(bound),
        "rename_paths": rename_paths,
        "preserve_on_error": preserve_on_error,
        "edit_dwg": edit_dwg,
    }


def _git(repo: Path, *arguments: str, output=None) -> bytes:
    try:
        result = subprocess.run(
            ["git", "--no-replace-objects", "-C", str(repo), *arguments],
            stdin=subprocess.DEVNULL,
            stdout=output if output is not None else subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        raise ValueError("Reading the requested Git snapshot failed.") from None
    return result.stdout or b""


def _commit(repo: Path, revision: str) -> str:
    if _git(repo, "rev-parse", "--show-prefix").strip():
        raise ValueError("The snapshot path must be the Git repository root.")
    identity = _git(repo, "rev-parse", "--verify", "--end-of-options", revision + "^{commit}").strip()
    if _HASH.fullmatch(identity) is None:
        raise ValueError("The requested Git revision did not resolve to a commit.")
    return identity.decode("ascii")


def _snapshot_entries(repo: Path, commit: str) -> list[tuple[PurePosixPath, str, bool]]:
    roots = sorted(_SNAPSHOT_DIRECTORIES | _SNAPSHOT_FILES | _SNAPSHOT_OPTIONAL_FILES)
    records = _git(repo, "ls-tree", "--full-tree", "-rz", commit, "--", *roots)
    result = []
    tracked_roots = set()
    casefolded = {}
    for record in records.rstrip(b"\0").split(b"\0") if records else ():
        header, _, name = record.partition(b"\t")
        mode, kind, object_id = header.split()
        if mode not in (b"100644", b"100755") or kind != b"blob" or _HASH.fullmatch(object_id) is None:
            raise ValueError("The tracked code snapshot contains an unsupported entry.")
        try:
            path = PurePosixPath(name.decode("utf-8"))
        except UnicodeDecodeError:
            raise ValueError("The tracked code snapshot contains an invalid path.") from None
        if not _safe_code_path(path):
            raise ValueError("The tracked code snapshot contains a path outside the source allowlist.")
        for part in (path, *path.parents):
            value = part.as_posix()
            if casefolded.setdefault(value.casefold(), value) != value:
                raise ValueError("The tracked code snapshot contains case-insensitive path collisions.")
        tracked_roots.add(path.parts[0])
        result.append((path, object_id.decode("ascii"), mode == b"100755"))
    if not (_SNAPSHOT_FILES | _SNAPSHOT_DIRECTORIES) <= tracked_roots:
        raise ValueError("The tracked revision lacks the required project files or directories.")
    return result


def _safe_code_path(path: PurePosixPath) -> bool:
    if path.as_posix() in _SNAPSHOT_OPTIONAL_FILES:
        return True
    if len(path.parts) == 1:
        return path.name in _SNAPSHOT_FILES
    return (
        bool(path.parts) and path.parts[0] in _SNAPSHOT_DIRECTORIES and path.suffix == ".py"
        and all(_CODE_COMPONENT.fullmatch(part) and part != ".." for part in path.parts)
        and not any(part.casefold() in _PRIVATE_COMPONENTS for part in path.parts[:-1])
    )


def _extras(staged: Path) -> str:
    try:
        with (staged / "pyproject.toml").open("rb") as project:
            extras = tomllib.load(project).get("project", {}).get("optional-dependencies", {})
    except (AttributeError, tomllib.TOMLDecodeError):
        raise ValueError("The tracked project configuration is invalid.") from None
    if not isinstance(extras, dict):
        raise ValueError("The tracked project configuration is invalid.")
    selected = sorted(set(extras) & {"pdf", "native", "solidworks"})
    return "[" + ",".join(selected) + "]" if selected else ""


def _publish(staged: Path, code: Path, readme: Path, instructions: str) -> None:
    _require_missing(code, readme)
    created = False
    try:
        handle = readme.open("xb")
        created = True
        with handle:
            handle.write(instructions.encode("utf-8"))
        _require_missing(code)
        staged.rename(code)
    except BaseException:
        if created:
            readme.unlink(missing_ok=True)
        raise


def _readme(report: dict[str, object], commit: str, large_files: int, extras: str) -> str:
    statuses = report["by_status"]
    options = report["run_options"]
    flags = []
    mib = 1024**2
    fraction = options is not None and options["max_edit_bytes"] % mib
    if options is not None:
        flags.extend([f"--workers {options['workers']}", f"--max-edit-mib {options['max_edit_bytes'] // mib}"])
        if options["rename_paths"]:
            flags.append("--rename-paths")
        if options["preserve_on_error"]:
            flags.append("--preserve-on-error")
        if options["edit_dwg"]:
            flags.append("--edit-dwg")
    else:
        if report["renamed"]:
            flags.append("--rename-paths")
        if statuses.get("adapter_failed"):
            flags.append("--preserve-on-error")

    command = "  python -m machina_deid run /path/to/input /path/to/new/corpus \\\n    --rules /path/to/private/rules.json"
    if flags:
        command += " " + " ".join(flags)
    if fraction:
        command = _programmatic_command(options)
    lines = [
        "Machina CAD de-identification",
        "",
        f"Code revision {commit}",
        "",
        "Reproduce the local run",
        "",
        "Use Python 3.11 or newer, a case-sensitive filesystem, the original input, and the identical",
        "reviewed rulebook. Set MACHINA_PSEUDONYM_KEY to the original private key for keyed rules.",
        "The rules and key are maintained in private storage.",
        "",
        "  curl -fsSLO https://machina-input.joinbuilders.org/get-input.sh",
        "  bash get-input.sh --dest /path/to/input",
        "  cp -R /path/to/attempt/code /path/to/private/machina-code",
        "  cd /path/to/private/machina-code",
        "  python3 -m venv /path/to/private/venv",
        "  . /path/to/private/venv/bin/activate",
        f"  python -m pip install '.{extras}'",
    ]
    if options is not None and options["edit_dwg"]:
        lines.extend([
            "  export ODA_FILE_CONVERTER=/path/to/local/ODAFileConverter",
            "  export MACHINA_ODA_CONVERTER='bash /path/to/private/machina-code/tools/oda-neutral.sh'",
            "Use the same locally installed ODA build and hidden display as the original run.",
        ])
    lines.append(command)
    if options is None:
        lines.extend([
            "",
            "This run report predates recorded run settings. Use the original workers and size cap",
            "from the local run. The example command includes rename and fallback flags evidenced by its counts.",
        ])
    if report["revision"] is None:
        lines.extend(["", "Verify the packaged Git revision against the local run checkout."])
    lines.extend([
        "",
        "Corpus coverage",
        "",
        f"Files {report['files']}",
        f"Output bytes {report['output_bytes']}",
        f"Renamed paths {report['renamed']}",
        f"Files above {SCORER_FILE_BYTES:,} bytes {large_files}",
        "",
        "Top-level file outcomes",
    ])
    lines.extend(f"  {label} {count}" for label, count in sorted(statuses.items()))
    if report["archive_member_status"]:
        lines.append("Archive member outcomes")
        lines.extend(f"  {label} {count}" for label, count in sorted(report["archive_member_status"].items()))
    if report["by_suffix"]:
        lines.append("Top-level outcomes by format")
        for suffix, counts in sorted(report["by_suffix"].items()):
            detail = ", ".join(f"{label} {count}" for label, count in sorted(counts.items()))
            lines.append(f"  {suffix} {detail}")
    lines.extend([
        "",
        "Limitations and exceptions",
        "",
        "Coverage depends on reviewed rules and format adapters. Source files counted as unsupported,",
        "over_limit, or adapter_failed were preserved and may retain identifiers. An unchanged result",
        "means no edit was applied. Empty source files were preserved where present.",
        "A source_pointer is a Git LFS pointer supplied in place of a model. The corpus retains",
        "the supplied pointer bytes. The corresponding geometry was absent from the source corpus.",
        "The tracked pipeline README describes format-specific coverage and validation limits.",
        "",
    ])
    return "\n".join(lines)


def _programmatic_command(options: dict[str, object]) -> str:
    dwg_option = ", edit_dwg=True" if options["edit_dwg"] else ""
    return "\n".join([
        "python - <<'PY'",
        "import json",
        "import os",
        "from pathlib import Path",
        "from machina_deid.pipeline import RunOptions, rewrite_corpus",
        "from machina_deid.policy import Rulebook",
        'rules = Rulebook.from_file("/path/to/private/rules.json", key=os.environ.get("MACHINA_PSEUDONYM_KEY"))',
        "options = RunOptions(",
        f"    workers={options['workers']}, max_edit_bytes={options['max_edit_bytes']},",
        f"    rename_paths={options['rename_paths']}, preserve_on_error={options['preserve_on_error']}{dwg_option},",
        ")",
        'print(json.dumps(rewrite_corpus(Path("/path/to/input"), Path("/path/to/new/corpus"), rules, options)))',
        "PY",
    ])
