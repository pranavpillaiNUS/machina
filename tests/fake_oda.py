"""Stand-in for ODAFileConverter: a fake DWG is b"<AC code>\\n" followed by DXF text."""

import os
import sys
import time
from pathlib import Path

CODES = {
    "ACAD13": "AC1012", "ACAD14": "AC1014", "ACAD2000": "AC1015", "ACAD2004": "AC1018",
    "ACAD2007": "AC1021", "ACAD2010": "AC1024", "ACAD2013": "AC1027", "ACAD2018": "AC1032",
}


def main(source_dir, target_dir, version, kind, _recurse, _audit, _pattern="*.*"):
    mode = os.environ.get("FAKE_ODA_MODE", "")
    if mode == "hang":
        time.sleep(60)
    if mode == "empty":
        return 0
    for source in sorted(Path(source_dir).iterdir()):
        data = source.read_bytes()
        if kind == "DXF":
            payload = data.split(b"\n", 1)[1]
        else:
            payload = CODES[version].encode("ascii") + b"\n" + data
        Path(target_dir, source.stem + "." + kind.lower()).write_bytes(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
