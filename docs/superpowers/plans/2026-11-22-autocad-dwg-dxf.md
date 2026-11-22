# AutoCAD DWG and DXF De-identification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pseudonymize every identity carrier in the corpus's 1,625 DXF and 758 DWG files while keeping them usable and their references resolvable.

**Architecture:** Move the existing line-level ASCII DXF rewriter into its own module. Extend it to cover header properties, object strings, MTEXT that needs re-chunking and preview thumbnails. Guard every edit with an invariant check that allows only string-field changes. DWG files go through the free ODA File Converter (DWG → DXF in the same version → the same DXF rewriter → DWG in the same version), followed by a per-file round-trip fidelity check. DWG editing is opt-in, like `--rename-paths`.

**Tech Stack:** Python ≥3.11 standard library and `unittest`; ODA File Converter (external binary) under `xvfb-run` on Linux. No ezdxf dependency: the existing record-level parser keeps byte-exact output for untouched records.

**Spec:** `README.md` ("Design and verification") and `reports/AutoCAD pseudonymization pipeline.md` (identity carriers, the recommended pipeline table and open questions 1–3). Research notes: `research_notes/AutoCAD pseudonymization pipeline/identity_locations.md` for the group codes used below.

## Global Constraints

- Python `>=3.11`; tests run with `python -m unittest discover -s tests -v`.
- No new required runtime dependency. ODA File Converter is optional and located at runtime.
- Adapter signature stays `(source: Path, destination: Path, replace: Callable[[str], str]) -> bool`: return False and leave `destination` alone when nothing changed.
- A file that needs no edit must come out byte-identical. A DWG that needs no edit must never pass through the converter's write path.
- Never change a DWG/DXF version: output `AC10xx` must equal input `AC10xx`.
- Error messages and reports must not contain source paths, file names or identity strings (match the existing `ValueError` style).
- Raw corpus, samples, probe reports and rules stay in git-ignored `input*/`, `private/` or `.local/`. Only synthetic fixtures are committed.
- Handles (group codes 5, 105, 320–369), `$HANDSEED`, layer names (8), block names (2) and layout names (410) are never edited in this plan.

## Review Focus

1. **An identity split across two MTEXT chunks** (code 3 then code 1). It must be replaced, because chunks are joined before matching. Pinned in Task 3.
2. **A converter that exits 0 but writes no file, or hangs.** It must raise a `ValueError` within the timeout, not stage an empty file. Pinned in Task 5.
3. **A DWG that needs no edits.** The output must equal the source byte for byte. Pinned in Task 7.
4. **A pre-R13 or unrecognised DWG signature.** It must raise, not be guessed at. Pinned in Task 5.
5. **A DXF whose header has `$CUSTOMPROPERTYTAG`/`$CUSTOMPROPERTY` pairs.** Values are rewritten in place, with order and count unchanged (AutoCAD depends on their position after `$LASTSAVEDBY`). Pinned in Task 2.

---

### Task 1: Move the DXF adapter into `machina_deid/dxf.py`

**Files:**
- Create: `machina_deid/dxf.py`
- Modify: `machina_deid/formats.py` (delete `_DXF_*`, `_split_lines`, `_dxf_encoding`, `_dxf_mtext`, `_rewrite_dxf`; `rewrite_file` calls `dxf.rewrite_dxf_bytes`; keep `_split_lines` importable if IGES uses it)
- Test: `tests/test_dxf.py`

**Interfaces:**
- Produces:
  - `DxfRecord = tuple[int, bytes]` (group code, value with surrounding whitespace stripped)
  - `read_records(data: bytes) -> list[DxfRecord]`: raises the existing ValueErrors (binary DXF, incomplete pair, malformed code, missing EOF)
  - `dxf_encoding(records: list[DxfRecord]) -> str`
  - `header_values(records: list[DxfRecord]) -> list[tuple[str, bytes]]`: `("$NAME", value)` for each header variable's first value record, in file order, repeats kept
  - `rewrite_dxf_bytes(data: bytes, replace: Replacement, *, strip_thumbnail: bool = True) -> bytes`

- [ ] **Step 1: Write the failing tests.** In `tests/test_dxf.py`, move the existing DXF case from `tests/test_formats.py` so it calls `rewrite_dxf_bytes` directly, and add:

```python
def test_read_records_and_header_values(self):
    data = b"  0\nSECTION\n  2\nHEADER\n  9\n$ACADVER\n  1\nAC1032\n  9\n$AUTHOR\n  1\nAna\n  0\nENDSEC\n  0\nEOF\n"
    records = read_records(data)
    self.assertEqual(records[0], (0, b"SECTION"))
    self.assertEqual(header_values(records), [("$ACADVER", b"AC1032"), ("$AUTHOR", b"Ana")])
    self.assertEqual(dxf_encoding(records), "utf-8")
```

- [ ] **Step 2: Run** `python -m unittest tests.test_dxf -v`. Expected: ImportError.
- [ ] **Step 3: Move the code.** Behaviour is unchanged apart from the new public names; `strip_thumbnail` is accepted and ignored until Task 4.
- [ ] **Step 4: Run** `python -m unittest discover -s tests -v`. Expected: all pass (48 existing tests plus the new one).
- [ ] **Step 5: Commit** `refactor: move DXF rewriting into its own module`

### Task 2: Rewrite header properties and object strings

**Files:**
- Modify: `machina_deid/dxf.py`
- Test: `tests/test_dxf.py`

**Interfaces:**
- Produces: `editable(record_type: bytes, code: int, header_name: bytes | None) -> bool`. This is the single table of fields the rewriter may change; Task 4's invariant check uses it too.

Values to add, from the identity-locations notes:

- Header names (only their group-1 value): `$TITLE $SUBJECT $AUTHOR $KEYWORDS $COMMENTS $LASTSAVEDBY $REVISIONNUMBER $CUSTOMPROPERTYTAG $CUSTOMPROPERTY $PROJECTNAME $HYPERLINKBASE $STYLESHEET $MENU`
- `XRECORD`, `FIELD`, `DICTIONARYVAR`: codes `{1, 2, 3, 4, 6, 7, 300–309, 430–439, 470–479}`, rewritten as plain text (no MTEXT parsing)
- `ACAD_TABLE`: codes `{1, 3}`, rewritten with MTEXT parsing
- Keep the existing entries: the visible-text table, code 1 of the path record types, and codes 999 and 1000 everywhere.

- [ ] **Step 1: Write the failing tests**

```python
def test_header_identity_and_custom_properties_rewrite_in_place(self):
    data = (b"  0\nSECTION\n  2\nHEADER\n  9\n$LASTSAVEDBY\n  1\nACME\n"
            b"  9\n$CUSTOMPROPERTYTAG\n  1\nClient\n  9\n$CUSTOMPROPERTY\n  1\nACME Ltd\n"
            b"  9\n$PROJECTNAME\n  1\nACME Tower\n  9\n$HANDSEED\n  5\nACME\n  0\nENDSEC\n  0\nEOF\n")
    out = rewrite_dxf_bytes(data, lambda s: s.replace("ACME", "NOVA"))
    self.assertEqual(out, data.replace(b"1\nACME", b"1\nNOVA").replace(b"ACME Ltd", b"NOVA Ltd")
                     .replace(b"ACME Tower", b"NOVA Tower"))
    self.assertIn(b"$HANDSEED\n  5\nACME", out)

def test_xrecord_field_and_table_strings(self):
    data = (b"  0\nXRECORD\n  5\n1F\n  1\nACME\n300\nACME\n  0\nFIELD\n  2\n%<\\AcVar Author>%\n  1\nACME\n"
            b"  0\nACAD_TABLE\n  2\n*T1\n  1\n{\\fArial;ACME}\n  8\nACME\n  0\nEOF\n")
    out = rewrite_dxf_bytes(data, lambda s: s.replace("ACME", "NOVA"))
    self.assertEqual(out.count(b"NOVA"), 4)
    self.assertIn(b"  5\n1F", out)
    self.assertIn(b"  8\nACME", out)
```

- [ ] **Step 2: Run** `python -m unittest tests.test_dxf -v`. Expected: both new tests FAIL.
- [ ] **Step 3: Implement `editable`.** Route the main loop through it. Track the current header name, which is the value of the preceding code-9 record inside the HEADER section.
- [ ] **Step 4: Run the full suite.** Expected: PASS.
- [ ] **Step 5: Commit** `feat: rewrite DXF header properties and object strings`

### Task 3: Re-chunk MTEXT instead of failing

**Files:**
- Modify: `machina_deid/dxf.py` (`rewrite_mtext`)
- Test: `tests/test_dxf.py`

At present a replacement that doesn't fit the existing code-3/code-1 byte widths raises, which aborts the whole run. Replace that with: join the chunks, rewrite, then emit `ceil(len/250)` records (code 3 for all but the last, code 1 for the last). Each chunk is at most 250 encoded bytes and is split only on a character boundary. Each new code line copies the indentation of the original code-3 line (or code-1 line). Line endings follow the record that held the first chunk.

- [ ] **Step 1: Write the failing tests**

```python
def test_mtext_identity_split_across_chunks_is_replaced(self):
    first = b"x" * 246 + b"AC"
    data = b"  0\nMTEXT\n  3\n" + first + b"\n  1\nME tail\n  0\nEOF\n"
    out = rewrite_dxf_bytes(data, lambda s: s.replace("ACME", "NOVA"))
    values = [v for c, v in read_records(out) if c in (1, 3)]
    self.assertEqual(b"".join(values), b"x" * 246 + b"NOVA tail")

def test_mtext_growth_adds_chunks_within_250_bytes(self):
    data = b"  0\nMTEXT\n  1\nACME\n  0\nEOF\n"
    out = rewrite_dxf_bytes(data, lambda s: s.replace("ACME", "é" * 300))
    records = [r for r in read_records(out) if r[0] in (1, 3)]
    self.assertEqual([c for c, _ in records], [3, 1])  # 300 one-byte cp1252 chars -> 250 + 50
    self.assertTrue(all(len(v) <= 250 for _, v in records))
    self.assertEqual(b"".join(v for _, v in records).decode("cp1252"), "é" * 300)
```


- [ ] **Step 2: Run.** Expected: FAIL with "does not fit the existing chunk structure".
- [ ] **Step 3: Implement.** Rebuild `lines` as a new list rather than mutating it by index once a record count changes.
- [ ] **Step 4: Run the full suite.** Expected: PASS.
- [ ] **Step 5: Commit** `fix: re-chunk MTEXT replacements instead of aborting`

### Task 4: Strip thumbnails and enforce edit invariants

**Files:**
- Modify: `machina_deid/dxf.py`
- Test: `tests/test_dxf.py`

**Interfaces:**
- Produces: `check_invariants(before: list[DxfRecord], after: list[DxfRecord]) -> None`. It raises `ValueError("DXF edit changed a protected field")`.

Thumbnails: when `strip_thumbnail` is set, drop every record from `0/SECTION, 2/THUMBNAILIMAGE` through its `0/ENDSEC`. The preview can show title blocks as pixels. Removing the section is valid DXF.

Invariant: normalise both record lists as follows, then require equal length, equal codes, and equal values wherever `editable(...)` is False.
- Remove the THUMBNAILIMAGE section from `before`.
- Collapse each MTEXT entity's code-3/code-1 run into one `(1, joined)` record, on both sides.

`rewrite_dxf_bytes` calls `check_invariants(read_records(data), read_records(output))` before returning a changed result.

- [ ] **Step 1: Write the failing tests**

```python
def test_thumbnail_section_is_removed(self):
    data = b"  0\nSECTION\n  2\nTHUMBNAILIMAGE\n 90\n2\n310\nFFFF\n  0\nENDSEC\n  0\nEOF\n"
    self.assertEqual(rewrite_dxf_bytes(data, str), b"  0\nEOF\n")
    self.assertEqual(rewrite_dxf_bytes(data, str, strip_thumbnail=False), data)

def test_invariants_reject_protected_change(self):
    before = [(0, b"TEXT"), (5, b"1F"), (1, b"ACME"), (0, b"EOF")]
    check_invariants(before, [(0, b"TEXT"), (5, b"1F"), (1, b"NOVA"), (0, b"EOF")])
    with self.assertRaisesRegex(ValueError, "protected field"):
        check_invariants(before, [(0, b"TEXT"), (5, b"20"), (1, b"NOVA"), (0, b"EOF")])
```

- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement both.**
- [ ] **Step 4: Run the full suite.** Expected: PASS.
- [ ] **Step 5: Commit** `feat: strip DXF thumbnails and verify edits touch only string fields`

### Task 5: ODA File Converter wrapper

**Files:**
- Create: `machina_deid/oda.py`, `tests/fake_oda.py` (test double)
- Test: `tests/test_oda.py`

**Interfaces:**
- Produces:
  - `DWG_VERSIONS: dict[str, str]` = `{"AC1012": "ACAD13", "AC1014": "ACAD14", "AC1015": "ACAD2000", "AC1018": "ACAD2004", "AC1021": "ACAD2007", "AC1024": "ACAD2010", "AC1027": "ACAD2013", "AC1032": "ACAD2018"}`
  - `dwg_version(path: Path) -> str`: reads the first 6 bytes and raises `ValueError("Unsupported DWG version")` for anything not in the table
  - `converter_command() -> list[str] | None`: uses `MACHINA_ODA_CONVERTER` (split with `shlex`), else `shutil.which("ODAFileConverter")`. On Linux, when there is no `DISPLAY` and `xvfb-run` exists, it prepends `["xvfb-run", "-a"]`.
  - `convert(source: Path, output_dir: Path, version: str, kind: Literal["DWG", "DXF"], *, timeout: float = 300) -> Path`

`convert` copies `source` alone into a fresh temp input dir, so that renamed or odd names never reach the converter. It then runs `command + [in_dir, output_dir, version, kind, "0", "1", "*.*"]` (positional arguments: input dir, output dir, version, type, recurse=0, audit=1, filter). It returns the single output with suffix `.dwg`/`.dxf`, and raises `ValueError` in three cases: a timeout (kill the process), a missing output, or an empty output. The converter can exit 0 without output, so the exit code is not trusted.

`tests/fake_oda.py` models a "DWG" as `b"AC1032\n" + dxf_bytes`. Type DXF strips the first line, and type DWG prepends `<version code>\n` (the reverse lookup of `DWG_VERSIONS`). The env var `FAKE_ODA_MODE=empty|hang` simulates the failures.

- [ ] **Step 1: Write the failing tests:** `test_dwg_version_known_and_rejected` (AC1032 → `"AC1032"`; `b"AC1009"` and `b"MC0.0"` raise), `test_convert_round_trip_with_fake`, `test_convert_empty_output_raises`, `test_convert_timeout_raises` (timeout=1, hang mode), and `test_command_uses_env_override`. Point `MACHINA_ODA_CONVERTER` at `f"{sys.executable} tests/fake_oda.py"` using an absolute path.
- [ ] **Step 2: Run** `python -m unittest tests.test_oda -v`. Expected: ImportError.
- [ ] **Step 3: Implement** `oda.py` with `subprocess.run(..., timeout=timeout, capture_output=True)`.
- [ ] **Step 4: Run the full suite.** Expected: PASS.
- [ ] **Step 5: Commit** `feat: wrap ODA File Converter for headless DWG conversion`

### Task 6: Measure ODA round-trip fidelity on real samples (gate)

**Files:**
- Create: `tools/oda_probe.py` (dev tool, not packaged)
- Modify: `.gitignore` (add `input-*/`)

This answers the report's open questions 1–3 before DWG editing ships.

- [ ] **Step 1: Install the converter.** Get the Linux x64 DEB from https://www.opendesign.com/guestfiles/oda_file_converter, plus `sudo apt install xvfb`. Record the version and licence text shown at install in `private/oda-licence.txt`. Stop and ask the user if the licence forbids this use.
- [ ] **Step 2: Download a sample of 40 DWG and 40 DXF files**, spread across repositories:
```sh
cut -f1 .local/manifest.tsv | grep -i '[.]dwg$' | shuf -n 40 --random-source=<(yes) > .local/dwg-sample.txt
cut -f1 .local/manifest.tsv | grep -i '[.]dxf$' | shuf -n 40 --random-source=<(yes) > .local/dxf-sample.txt
cat .local/dwg-sample.txt .local/dxf-sample.txt | while read -r p; do bash get-input.sh --prefix "$p" --dest input-acad; done
```
- [ ] **Step 3: Write** `tools/oda_probe.py INPUT_DIR REPORT_JSON`. For each DWG, using `oda.convert`, it converts to DXF in the same version (A) and A to DWG to DXF again (B). It also does a canary pass: set `$LASTSAVEDBY` and the first BLOCK code-1 path in A to `MACHINA-CANARY`, then convert to DWG and back to DXF (C). Each file's report records:
  - version, and whether the output version is unchanged
  - seconds per conversion
  - whether the counts of code-0 record types and the code-5 handle sets match between A and B
  - whether THUMBNAILIMAGE appears in A and in B
  - whether the canary values are present in C
  - the B/A size ratio

  For DXF inputs, it reports how many are binary (`AutoCAD Binary DXF` prefix) and how many `rewrite_dxf_bytes(data, str)` would raise on. The report holds counts and ratios only, keyed by sample index rather than path.
- [ ] **Step 4: Run it** and write a 10-line summary of the aggregates into `docs/autocad-probe.md` (no paths or names).
- [ ] **Step 5: Gate.** Continue to Task 7 only if at least 95% of DWG samples match in both type counts and handle sets and the canary survives in C. Otherwise stop and bring the numbers to the user: the fallback is the ODA SDK or a Windows AcCoreConsole step, per the report.
- [ ] **Step 6: Commit** `tools/oda_probe.py`, `docs/autocad-probe.md`, `.gitignore` with the message `chore: measure ODA DWG round-trip fidelity`

### Task 7: DWG adapter, pipeline flag and DWG candidates

**Files:**
- Create: `machina_deid/dwg.py`
- Modify: `machina_deid/pipeline.py` (`RunOptions.edit_dwg: bool = False`; `_select_adapter(source, suffix, *, edit_dwg: bool)` at both call sites; `run_options` report key), `machina_deid/__main__.py` (`--edit-dwg`), `machina_deid/candidates.py`
- Test: `tests/test_dwg.py`, `tests/test_pipeline.py`, `tests/test_candidates.py`

**Interfaces:**
- Consumes: `oda.dwg_version`, `oda.convert`, `oda.converter_command`, `dxf.rewrite_dxf_bytes`, `dxf.read_records`, `dxf.header_values`
- Produces: `rewrite_dwg(source: Path, destination: Path, replace: Replacement) -> bool`

Flow inside a `TemporaryDirectory`:
1. `v = dwg_version(source)`
2. A = `convert(source, tmp, DWG_VERSIONS[v], "DXF")`
3. `edited = rewrite_dxf_bytes(A)`. If it is unchanged, return False.
4. Write the edited DXF and `convert` it to DWG.
5. Require `dwg_version(out) == v`.
6. Convert the output back to DXF (B). Require that B has the same code-0 type counts and code-5 handle set as A, and that `rewrite_dxf_bytes(B, replace, strip_thumbnail=False) == B` (nothing replaceable remains).
7. Copy to `destination` and return True.

Any failed check raises `ValueError("DWG round trip changed drawing structure")` or `ValueError("DWG still contains replaceable text")`. If no converter is found, `rewrite_dwg` raises `ValueError("DWG editing needs ODA File Converter")`. `rewrite_corpus` checks `converter_command()` up front when `edit_dwg` is set and fails before staging.

Candidates: when `converter_command()` is available, inspect `.dwg` through a DXF conversion; always inspect `.dxf`. Evidence tuples, using the existing `(kind, origin, value)` shape:
- `("author", "dxf.$AUTHOR", v)`
- `("author", "dxf.$LASTSAVEDBY", v)`
- `("title", "dxf.$TITLE", v)`
- `("project", "dxf.$PROJECTNAME", v)`
- `("custom", "dxf.$CUSTOMPROPERTY", v)`

Count them under `inspected["dxf"]`/`inspected["dwg"]` and `skipped["dwg_converter_unavailable"]`.

- [ ] **Step 1: Write the failing tests.** All of them use `tests/fake_oda.py` through `MACHINA_ODA_CONVERTER`.
  - `test_unchanged_dwg_returns_false_and_leaves_destination`
  - `test_dwg_text_rewritten_in_same_version`: the output starts with `b"AC1032"` and contains `NOVA` but not `ACME`
  - `test_unknown_version_raises`
  - in `test_pipeline.py`: a `.dwg` is `unsupported` without `edit_dwg` and `rewritten` with it, and its BLOCK xref path follows `--rename-paths`
  - in `test_candidates.py`: a DXF with `$AUTHOR`/`$LASTSAVEDBY` produces `author` candidates with origin `dxf.$AUTHOR`
- [ ] **Step 2: Run** `python -m unittest tests.test_dwg tests.test_pipeline tests.test_candidates -v`. Expected: the new tests FAIL.
- [ ] **Step 3: Implement** `dwg.py`, the pipeline flag, the CLI flag and the candidate branch.
- [ ] **Step 4: Run the full suite.** Expected: PASS. Then run against the real sample: `python -m machina_deid run input-acad output/acad-1 --rules private/rules.json --edit-dwg --preserve-on-error`. Expected: no `adapter_failed` for `.dxf`; any `.dwg` failure rate is within the Task 6 numbers.
- [ ] **Step 5: Update the README.** Add a "DWG and DXF" paragraph to "Run the pipeline" covering the ODA install, `--edit-dwg`, what is rewritten, what is never touched (handles, layer, block and layout names, OLE, proxies), and the loss of the TrustedDWG marker.
- [ ] **Step 6: Commit** `feat: de-identify DWG through ODA round trips with fidelity checks`

## Out of scope (tracked for the next plan)

- Renaming identifying layer, block and xref-dependent `XREF|LAYER` names. This needs every host rewritten in step, per the report.
- Binary DXF (counted by the Task 6 probe).
- OLE2FRAME and VBA payloads, and proxy-object text.
- A residual-identity scan via LibreDWG `dwgread` JSON, and a visual diff of rendered layouts.
