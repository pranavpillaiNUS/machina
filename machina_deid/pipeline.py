"""Plan and stage a corpus rewrite before publishing it locally."""

from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import partial
from itertools import chain
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil
from tempfile import TemporaryDirectory

from .archives import ArchiveLimits, MemberTransform, archive_suffix, rewrite_archive
from .corpus import CorpusFile, filesystem_path, scan
from .discovery import classify_suffix
from .formats import SUPPORTED_SUFFIXES as CAD_SUFFIXES
from .formats import rewrite_file
from .geometry_text import SUPPORTED_SUFFIXES as GEOMETRY_SUFFIXES
from .geometry_text import rewrite_geometry_text
from .policy import PathMap, Rulebook
from .svg import SUPPORTED_SUFFIXES as SVG_SUFFIXES
from .svg import rewrite_svg


ADAPTERS = {
    suffix: adapter
    for suffixes, adapter in (
        (CAD_SUFFIXES, rewrite_file),
        (GEOMETRY_SUFFIXES, rewrite_geometry_text),
        (SVG_SUFFIXES, rewrite_svg),
    )
    for suffix in suffixes
}


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
    archive_members: Counter[str] = field(default_factory=Counter)


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
    rulebook: Rulebook,
    options: RunOptions,
) -> FileResult:
    file, mapped = item
    source = source_root.joinpath(*file.relative.parts)
    target = staging.joinpath(*mapped.parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    suffix = classify_suffix(file.relative.name)
    archive = archive_suffix(file.relative.name)
    adapter = ADAPTERS.get(suffix)
    archive_members: Counter[str] = Counter()
    if file.size == 0:
        status = "empty"
    elif not (adapter or archive):
        status = "unsupported"
    elif file.size > (ArchiveLimits().max_member_bytes if archive else options.max_edit_bytes):
        status = "over_limit"
    else:
        replace = partial(path_map.replace, source_path=file.relative)
        try:
            if archive:
                changed = _rewrite_embedded(source, target, rulebook, options, archive_members)
            else:
                changed = adapter(source, target, replace)
        except Exception as error:
            if not options.preserve_on_error:
                raise ValueError(
                    f"The {suffix} adapter failed ({type(error).__name__}). "
                    "An incomplete corpus was discarded."
                ) from None
            target.unlink(missing_ok=True)
            archive_members.clear()
            status = "adapter_failed"
        else:
            status = "rewritten" if changed else "unchanged"
    if status != "rewritten" and not target.exists():
        shutil.copyfile(source, target)
    return FileResult(suffix, status, target.stat().st_size, file.relative != mapped, archive_members)


def _rewrite_embedded(
    source: Path,
    target: Path,
    rulebook: Rulebook,
    options: RunOptions,
    counts: Counter[str],
) -> bool:
    def make_transform(names: tuple[str, ...]) -> MemberTransform:
        # Preserve member names and references within the archive's own namespace.
        paths = PathMap.build(names, rulebook, rename_paths=False)

        def transform(name: str, original: Path, replacement: Path) -> bool:
            size = original.stat().st_size
            adapter = ADAPTERS.get(classify_suffix(name))
            if size == 0:
                counts["empty"] += 1
                return False
            if adapter is None or size > options.max_edit_bytes:
                counts["unsupported" if adapter is None else "over_limit"] += 1
                return False
            replace = partial(paths.replace, source_path=PurePosixPath(name))
            try:
                changed = adapter(original, replacement, replace)
            except Exception:
                if not options.preserve_on_error:
                    raise
                replacement.unlink(missing_ok=True)
                counts["adapter_failed"] += 1
                return False
            counts["rewritten" if changed else "unchanged"] += 1
            return changed

        return transform

    return rewrite_archive(source, target, make_transform=make_transform).changed


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
    archive_members: Counter[str] = Counter()
    by_suffix: dict[str, Counter[str]] = defaultdict(Counter)
    size = renamed = 0

    with TemporaryDirectory(prefix=".machina-stage-", dir=output_root.parent) as scratch:
        staging = Path(scratch) / "corpus"
        staging.mkdir()
        _validate_destination_paths(destinations, staging)
        process = partial(
            _rewrite_one,
            source_root=input_root,
            staging=staging,
            path_map=path_map,
            rulebook=rulebook,
            options=options,
        )
        batch = options.workers * 8
        with ThreadPoolExecutor(max_workers=options.workers) as pool:
            for start in range(0, len(files), batch):
                pending = zip(files[start : start + batch], destinations[start : start + batch])
                parallel = []
                serial = []
                for job in pending:
                    # Archives can also contain PDFs. MuPDF needs one thread.
                    name = job[0].relative.name
                    jobs = serial if name.lower().endswith(".pdf") or archive_suffix(name) else parallel
                    jobs.append(job)
                background = pool.map(process, parallel)
                for result in chain(map(process, serial), background):
                    statuses[result.status] += 1
                    by_suffix[result.suffix][result.status] += 1
                    size += result.size
                    renamed += result.renamed
                    archive_members.update(result.archive_members)
        if output_root.exists() or output_root.is_symlink():
            raise ValueError("Output was created during the run. Choose a new output directory.")
        staging.rename(output_root)

    return {
        "files": len(files),
        "output_bytes": size,
        "renamed": renamed,
        "run_options": {
            "workers": options.workers,
            "max_edit_mib": options.max_edit_bytes / 1024**2,
            "rename_paths": options.rename_paths,
            "preserve_on_error": options.preserve_on_error,
        },
        "by_status": dict(sorted(statuses.items())),
        "archive_member_status": dict(sorted(archive_members.items())),
        "by_suffix": {suffix: dict(sorted(counts.items())) for suffix, counts in sorted(by_suffix.items())},
    }
