"""De-identify DWG through a same-version ODA round trip with fidelity checks."""

from __future__ import annotations

import os
import pwd
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory

from .dxf import Replacement, drawing_shape, header_values, read_records, rewrite_dxf_bytes
from .oda import DWG_VERSIONS, convert, converter_command, dwg_version

SUPPORTED_SUFFIXES = frozenset({".dwg"})


def rewrite_dwg(source: Path, destination: Path, replace: Replacement) -> bool:
    """Rewrite every DWG.

    ODA replaces $LASTSAVEDBY when it exports a DWG, so the source value cannot be
    inspected. Writing every file replaces it with the converter's account name.
    """
    version = dwg_version(source)
    if converter_command() is None:
        raise ValueError("DWG editing needs ODA File Converter with a hidden display")
    target = DWG_VERSIONS[version]
    with TemporaryDirectory(prefix="machina-dwg-") as folder:
        work = Path(folder)
        original_path = convert(source, work, target, "DXF")
        original = original_path.read_bytes()
        # ODA normalizes on every write: it renumbers anonymous blocks, drops default
        # values and re-encodes proxy graphics. An unedited round trip is the baseline,
        # so the edited round trip may differ from it only by the edits themselves.
        baseline = convert(convert(original_path, work, target, "DWG"), work, target, "DXF").read_bytes()
        edited = work / "edited.dxf"
        edited.write_bytes(rewrite_dxf_bytes(original, replace))
        output = convert(edited, work, target, "DWG")
        if dwg_version(output) != version:
            raise ValueError("DWG conversion changed the file version")
        check = convert(output, work, target, "DXF").read_bytes()
        check_records = read_records(check)
        expected = read_records(rewrite_dxf_bytes(baseline, replace))
        if drawing_shape(check_records) != drawing_shape(expected):
            raise ValueError("DWG round trip changed drawing structure")
        if rewrite_dxf_bytes(check, replace, strip_thumbnail=False) != check:
            raise ValueError("DWG still contains replaceable text")
        saved_by = dict(header_values(check_records)).get("$LASTSAVEDBY")
        if saved_by == pwd.getpwuid(os.getuid()).pw_name.encode():
            raise ValueError("DWG output carries the local login name; run the converter under a neutral account")
        shutil.copyfile(output, destination)
    return True
