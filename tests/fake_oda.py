"""Stand-in for ODAFileConverter: a fake DWG is b"<AC code>\\n" followed by DXF text."""

import os
import pwd
import re
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
        if os.environ.get("FAKE_ODA_MUTATE") and kind == "DWG" and b"NOVA" in payload:
            # Imitate a round trip that loses data only when edited content is written.
            payload = payload.replace(b" 10\n1.0\n", b" 10\n999.0\n")
        if os.environ.get("FAKE_ODA_STAMP"):
            # Imitate ODA: every write records the passwd login as $LASTSAVEDBY.
            login = pwd.getpwuid(os.getuid()).pw_name.encode()
            payload = re.sub(rb"(\$LASTSAVEDBY\r?\n *1\r?\n)[^\r\n]*", lambda m: m[1] + login, payload)
        Path(target_dir, source.stem + "." + kind.lower()).write_bytes(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
