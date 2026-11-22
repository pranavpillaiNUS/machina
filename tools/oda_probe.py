"""Measure ODA File Converter round-trip fidelity on local DWG/DXF samples.

Usage: python tools/oda_probe.py INPUT_DIR REPORT_JSON

The report holds counts and ratios keyed by sample index, never paths or names.
"""

from __future__ import annotations

import json
import os
import pwd
import sys
import tempfile
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from machina_deid.dxf import drawing_shape, header_values, read_records, rewrite_dxf_bytes
from machina_deid.oda import DWG_VERSIONS, convert, dwg_version

CANARY = "MACHINA-CANARY"
LOGIN = pwd.getpwuid(os.getuid()).pw_name.encode()


def _shape(data: bytes):
    return drawing_shape(read_records(data))


def _has_thumbnail(data: bytes) -> bool:
    return b"THUMBNAILIMAGE" in data


def probe_dwg(index: int, source: Path) -> dict:
    row: dict = {"index": index}
    try:
        version = dwg_version(source)
        target = DWG_VERSIONS[version]
        row["version"] = version
        with tempfile.TemporaryDirectory() as folder:
            work = Path(folder)
            started = time.monotonic()
            a_path = convert(source, work, target, "DXF")
            a = a_path.read_bytes()
            dwg_b = convert(a_path, work, target, "DWG")
            b = convert(dwg_b, work, target, "DXF").read_bytes()
            row["seconds_per_conversion"] = round((time.monotonic() - started) / 3, 2)
            row["version_kept"] = dwg_version(dwg_b) == version
            row["size_ratio"] = round(dwg_b.stat().st_size / source.stat().st_size, 3)
            (types_a, handles_a, _), (types_b, handles_b, _) = _shape(a), _shape(b)
            row["types_equal"] = types_a == types_b
            row["handles_equal"] = handles_a == handles_b
            row["thumbnail_a"] = _has_thumbnail(a)
            row["thumbnail_b"] = _has_thumbnail(b)
            header = header_values(read_records(a))
            row["login_in_a"] = LOGIN in a
            customs = [value for name, value in header if name == "$CUSTOMPROPERTY" and value.strip()]
            row["has_custom_property"] = bool(customs)
            xrefs = [v for c, v in _block_paths(a)]
            row["xref_paths"] = len(xrefs)
            if customs or xrefs:
                targets = [customs[0].decode("utf-8", errors="replace")] if customs else []
                targets += [x.decode("utf-8", errors="replace") for x in xrefs[:1]]

                def canary(value: str) -> str:
                    for item in targets:
                        value = value.replace(item, CANARY)
                    return value

                edited = work / "edited.dxf"
                edited.write_bytes(rewrite_dxf_bytes(a, canary))
                c = convert(convert(edited, work, target, "DWG"), work, target, "DXF").read_bytes()
                c_header = header_values(read_records(c))
                row["canary_custom_property"] = (
                    any(v == CANARY.encode() for n, v in c_header if n == "$CUSTOMPROPERTY") if customs else None
                )
                row["login_in_c"] = LOGIN in c
                row["canary_xref"] = (CANARY.encode() in b"".join(v for _, v in _block_paths(c))) if xrefs else None
                row["thumbnail_after_strip"] = _has_thumbnail(c)
    except Exception as error:  # noqa: BLE001 - the probe records every failure class
        row["error"] = type(error).__name__ + ": " + str(error)[:80]
    return row


def _block_paths(data: bytes) -> list[tuple[int, bytes]]:
    paths, record_type = [], b""
    for code, value in read_records(data):
        if code == 0:
            record_type = value
        elif record_type == b"BLOCK" and code == 1 and value:
            paths.append((code, value))
    return paths


def probe_dxf(index: int, source: Path) -> dict:
    data = source.read_bytes()
    row = {"index": index, "binary": data.startswith(b"AutoCAD Binary DXF")}
    if not row["binary"]:
        try:
            rewrite_dxf_bytes(data, str)
            row["rewrite_ok"] = True
        except ValueError as error:
            row["rewrite_ok"] = False
            row["error"] = str(error)[:80]
    return row


def _share(rows: list[dict], key: str) -> str:
    values = [row[key] for row in rows if row.get(key) is not None]
    return f"{sum(map(bool, values))}/{len(values)}"


def main(input_dir: str, report_path: str) -> int:
    files = sorted(p for p in Path(input_dir).rglob("*") if p.is_file())
    dwgs = [p for p in files if p.suffix.lower() == ".dwg"]
    dxfs = [p for p in files if p.suffix.lower() == ".dxf"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        dwg_rows = list(pool.map(probe_dwg, range(len(dwgs)), dwgs))
    dxf_rows = [probe_dxf(i, p) for i, p in enumerate(dxfs)]
    ok = [row for row in dwg_rows if "error" not in row]
    summary = {
        "dwg_samples": len(dwg_rows),
        "dwg_errors": Counter(row["error"].split(":")[0] for row in dwg_rows if "error" in row),
        "dwg_versions": Counter(row.get("version", "unknown") for row in dwg_rows),
        **{key: _share(ok, key) for key in (
            "version_kept", "types_equal", "handles_equal", "thumbnail_a", "thumbnail_b",
            "thumbnail_after_strip", "canary_custom_property", "canary_xref", "login_in_a", "login_in_c",
        )},
        "median_seconds_per_conversion": sorted(r["seconds_per_conversion"] for r in ok)[len(ok) // 2] if ok else None,
        "size_ratio_range": [min(r["size_ratio"] for r in ok), max(r["size_ratio"] for r in ok)] if ok else None,
        "dxf_samples": len(dxf_rows),
        "dxf_binary": sum(row["binary"] for row in dxf_rows),
        "dxf_rewrite_ok": _share(dxf_rows, "rewrite_ok"),
        "dxf_errors": Counter(row["error"] for row in dxf_rows if "error" in row),
    }
    Path(report_path).write_text(json.dumps({"summary": summary, "dwg": dwg_rows, "dxf": dxf_rows}, indent=2))
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
