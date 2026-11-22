# ODA round-trip probe (2026-11-22)

Sample: 40 DWG and 40 DXF files drawn from the official manifest with a seeded shuffle. Converter: ODA File Converter AppImage (QT6, lnxX64), run by `tools/oda_probe.py` through `tools/oda-neutral.sh`.

- DWG versions: 20 AC1032, 11 AC1027, 4 AC1015, 3 AC1018, 1 AC1024, 1 AC1021. There were no conversion errors, and the median conversion took 0.6 s.
- A DWG → DXF → DWG round trip kept the version in 40 of 40 files. Object-type counts matched in 40 of 40, excluding CLASS records, where AC1015 files gain one. Handles in ENTITIES, BLOCKS and TABLES matched in 40 of 40.
- ODA gives new handles to some dictionaries and XRECORDs in the OBJECTS section on every write. Fidelity checks therefore compare drawing handles only.
- ODA stamps `$LASTSAVEDBY` with the passwd login name of the user running it on every DXF or DWG write. `USER` and `LOGNAME` do not change this. `tools/oda-neutral.sh` runs the converter under bubblewrap with a substituted passwd entry, so the output shows `machina`. Without the wrapper, every output leaks the operator's login.
- Edited custom properties survived DXF → DWG → DXF in 10 of 10 files.
- 14 of 40 DWGs carry a preview thumbnail. Stripping THUMBNAILIMAGE from the DXF produces DWGs without a preview in 10 of 10 files.
- The sample contained no xref paths, so xref repathing through DWG is untested on real files.
- DXF: 0 of 40 are binary, and `rewrite_dxf_bytes` parsed all 40 without error.
- Output and input DWG sizes differ by a factor of 0.30 to 3.66, so size is not a validity signal.
