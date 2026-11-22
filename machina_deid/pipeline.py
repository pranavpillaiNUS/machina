"""Plan and stage a corpus rewrite before publishing it locally."""

from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from itertools import chain
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil
from tempfile import TemporaryDirectory

from .corpus import CorpusFile, filesystem_path, scan
from .discovery import classify_suffix
from .formats import SUPPORTED_SUFFIXES, rewrite_file
from .policy import PathMap, Rulebook


@dataclass(frozen=True, slots=True)
class RunOptions:
    workers: int = 4
    max_edit_bytes: int = 64 * 1024**2
    rename_paths: bool = False
    preserve_on_error: bool = False


@dataclass(frozen=True, slots=True)
class FileResult:
    suffix: str
    status: str
    size: int
    renamed: bool


def _validate_destination_paths(destinations: list[PurePosixPath], staging: Path) -> None:
    probe = staging / "SensitivityProbe"
    probe.touch()
    insensitive = probe.with_name(probe.name.lower()).exists()
    probe.unlink()
    spellings: dict[str, str] = {}
    for destination in destinations:
        if os.name == "nt":
            for part in destination.parts:
                if (
                    part.endswith((" ", "."))
                    or PureWindowsPath(part).is_reserved()
                    or any(ord(char) < 32 or char in '<>:"|?*' for char in part)
                ):
                    raise ValueError("An output path is not representable on Windows.")
        if insensitive:
            for path in (destination, *destination.parents):
                value = path.as_posix()
                if spellings.setdefault(value.casefold(), value) != value:
                    raise ValueError("Output paths collide on a case-insensitive filesystem.")


def _rewrite_one(
    item: tuple[CorpusFile, PurePosixPath],
    source_root: Path,
    staging: Path,
    path_map: PathMap,
    options: RunOptions,
) -> FileResult:
    file, mapped = item
    source = source_root.joinpath(*file.relative.parts)
    target = staging.joinpath(*mapped.parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    suffix = classify_suffix(file.relative.name)
    if file.size == 0:
        status = "empty"
    elif suffix not in SUPPORTED_SUFFIXES:
        status = "unsupported"
    elif file.size > options.max_edit_bytes:
        status = "over_limit"
    else:
        replace = partial(path_map.replace, source_path=file.relative)
        try:
            changed = rewrite_file(source, target, replace)
        except Exception as error:
            if not options.preserve_on_error:
                raise ValueError(
                    f"The {suffix} adapter failed ({type(error).__name__}). "
                    "An incomplete corpus was discarded."
                ) from None
            target.unlink(missing_ok=True)
            status = "adapter_failed"
        else:
            status = "rewritten" if changed else "unchanged"
    if status != "rewritten":
        shutil.copyfile(source, target)
    return FileResult(suffix, status, target.stat().st_size, file.relative != mapped)


def rewrite_corpus(
    input_root: Path,
    output_root: Path,
    rulebook: Rulebook,
    options: RunOptions = RunOptions(),
) -> dict[str, object]:
    """Build a new corpus directory and return counts without original paths."""
    if not 1 <= options.workers <= 32 or options.max_edit_bytes < 1:
        raise ValueError("Use 1 to 32 workers and a positive edit size limit.")
    if input_root.is_symlink() or output_root.is_symlink():
        raise ValueError("Input and output roots must be real directories.")
    input_root = input_root.resolve(strict=True)
    output_root = output_root.resolve()
    if (
        input_root == output_root
        or input_root in output_root.parents
        or output_root in input_root.parents
    ):
        raise ValueError("Input and output directories must be disjoint.")
    if output_root.exists():
        raise ValueError("Output already exists. Choose a new output directory.")

    input_root = filesystem_path(input_root)
    output_root = filesystem_path(output_root)
    files = scan(input_root)
    if not files:
        raise ValueError("The input contains no corpus files.")
    path_map = PathMap.build(
        (file.relative for file in files), rulebook, rename_paths=options.rename_paths
    )
    destinations = [path_map[file.relative] for file in files]
    output_root.parent.mkdir(parents=True, exist_ok=True)
    statuses: Counter[str] = Counter()
    by_suffix: dict[str, Counter[str]] = defaultdict(Counter)
    size = renamed = 0

    with TemporaryDirectory(prefix=".machina-stage-", dir=output_root.parent) as scratch:
        staging = Path(scratch) / "corpus"
        staging.mkdir()
        _validate_destination_paths(destinations, staging)
        process = partial(
            _rewrite_one, source_root=input_root, staging=staging, path_map=path_map, options=options
        )
        batch = options.workers * 8
        with ThreadPoolExecutor(max_workers=options.workers) as pool:
            for start in range(0, len(files), batch):
                pending = zip(files[start : start + batch], destinations[start : start + batch])
                parallel = []
                pdfs = []
                for job in pending:
                    # MuPDF calls must run on one thread.
                    jobs = pdfs if job[0].relative.suffix.lower() == ".pdf" else parallel
                    jobs.append(job)
                background = pool.map(process, parallel)
                for result in chain(map(process, pdfs), background):
                    statuses[result.status] += 1
                    by_suffix[result.suffix][result.status] += 1
                    size += result.size
                    renamed += result.renamed
        if output_root.exists() or output_root.is_symlink():
            raise ValueError("Output was created during the run. Choose a new output directory.")
        staging.rename(output_root)

    return {
        "files": len(files),
        "output_bytes": size,
        "renamed": renamed,
        "by_status": dict(sorted(statuses.items())),
        "by_suffix": {suffix: dict(sorted(counts.items())) for suffix, counts in sorted(by_suffix.items())},
    }
