"""Run the external ODA File Converter headlessly for DWG and DXF conversion."""

from __future__ import annotations

import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Literal

DWG_VERSIONS = {
    "AC1012": "ACAD13",
    "AC1014": "ACAD14",
    "AC1015": "ACAD2000",
    "AC1018": "ACAD2004",
    "AC1021": "ACAD2007",
    "AC1024": "ACAD2010",
    "AC1027": "ACAD2013",
    "AC1032": "ACAD2018",
}


def dwg_version(path: Path) -> str:
    """Return the AC10xx signature of a DWG file or raise for unsupported versions."""
    with path.open("rb") as stream:
        version = stream.read(6).decode("ascii", errors="replace")
    if version not in DWG_VERSIONS:
        raise ValueError("Unsupported DWG version")
    return version


def converter_command() -> list[str] | None:
    """Locate the converter, or None when it cannot run without a visible window.

    An explicit override is used verbatim. The converter on PATH runs only on a
    hidden display: MACHINA_ODA_DISPLAY, else xvfb-run. A desktop DISPLAY is never used.
    """
    override = os.environ.get("MACHINA_ODA_CONVERTER")
    if override:
        return shlex.split(override)
    found = shutil.which("ODAFileConverter")
    if found is None:
        return None
    if os.environ.get("MACHINA_ODA_DISPLAY") or not sys.platform.startswith("linux"):
        return [found]
    if shutil.which("xvfb-run"):
        return ["xvfb-run", "-a", found]
    return None


def _child_environment() -> dict[str, str]:
    environment = dict(os.environ)
    hidden = environment.get("MACHINA_ODA_DISPLAY")
    if hidden:
        environment["DISPLAY"] = hidden
        environment.pop("WAYLAND_DISPLAY", None)
    return environment


def convert(
    source: Path,
    output_dir: Path,
    version: str,
    kind: Literal["DWG", "DXF"],
    *,
    timeout: float = 300,
) -> Path:
    """Convert one file and return the single non-empty output inside output_dir."""
    command = converter_command()
    if command is None:
        raise ValueError("DWG editing needs ODA File Converter with a hidden display")
    if version not in DWG_VERSIONS.values() or kind not in ("DWG", "DXF"):
        raise ValueError("Unsupported conversion target")
    target = Path(tempfile.mkdtemp(prefix="oda-out-", dir=output_dir))
    with tempfile.TemporaryDirectory(prefix="oda-in-") as staging:
        # Only a neutral name reaches the converter.
        shutil.copyfile(source, Path(staging, "drawing" + source.suffix.lower()))
        process = subprocess.Popen(
            [*command, staging, str(target), version, kind, "0", "1", "*.*"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=_child_environment(),
        )
        try:
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise ValueError("DWG conversion timed out") from None
    outputs = [
        path for path in target.iterdir()
        if path.suffix.lower() == "." + kind.lower() and path.stat().st_size > 0
    ]
    if returncode != 0 or len(outputs) != 1:
        raise ValueError("DWG conversion produced no output")
    return outputs[0]
