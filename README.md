# Machina CAD de-identification

This project builds a reproducible pipeline for the [Builders Buildathon CAD De-ID track](https://www.joinbuilders.org/buildathon/tracks/machina-deid). The goal is to replace identities consistently while preserving engineering data, file usability, and inter-file references.

## Get started

Use Python 3.11 or newer. Run commands from the repo root.

```sh
python -m machina_deid inventory input
```

The inventory reports file counts and sizes by extension, empty files, and case-insensitive path conflicts without listing original paths. It excludes the organizer's `input/.get-input/` download metadata. Inspect the official manifest before downloading the corpus.

```sh
mkdir -p .local
curl -fsSL https://machina-input.joinbuilders.org/manifest.tsv -o .local/manifest.tsv
python -m machina_deid inventory .local/manifest.tsv --manifest
```

Use a case-sensitive data filesystem with room for the input, output, and archive scratch data. The track recommends at least 400 GB for a cloud VM. The official downloader supports a small first run.

```sh
curl -fsSLO https://machina-input.joinbuilders.org/get-input.sh
bash get-input.sh --limit 100
python -m machina_deid inventory input
```

Keep the raw corpus, original identifiers, private rules, access tokens, and run logs local. Git ignores the standard data directories `input/`, `output/`, `private/`, `.local/`, and `attempt-*/`. Put additions to the pipeline and synthetic fixtures in Git.

## Design and verification

1. Inventory the official manifest and local samples. Rank formats by file count and bytes, identify empty files and duplicate source families, and check path collisions before processing.
2. Collect identity candidates from typed metadata, paths, and title blocks. Use one deterministic policy for aliases and approved replacements. Keep source-specific rules private.
3. Plan output paths before writing. Preserve original extensions and non-identifying path segments. Apply renames together with reference updates and validate that known references still resolve.
4. Rewrite named fields through format-specific adapters. Verify the result by reopening it and checking structural invariants, such as CAD entity IDs and numeric fields, PDF page boxes and rendering, and archive members and checksums. Report every format or file that could not be validated.
5. Package `corpus/`, `code/`, and `README.txt` through the official track workflow. Use category-level grading results to prioritize changes that can be measured in the next attempt.

## Run the pipeline

Install the optional PDF dependency when processing PDFs. The pipeline source is MIT. PyMuPDF has its own AGPL and commercial licensing options. [PyMuPDF licensing](https://pymupdf.io/licensing)

```sh
python -m pip install -e '.[pdf,native]'
python -m machina_deid candidates input --private-output private/candidates.json
python -m machina_deid run input output/dev/corpus --rules private/rules.json
python -m unittest discover -s tests -v
```

Candidate discovery reads bounded STEP headers and supported PDF, AutoCAD, and modern SolidWorks metadata. It writes exact spellings and evidence counts to the private review file and prints only aggregate counts. Review the candidates before adding approved aliases and replacements to the local rules file.

```json
{
  "rules": [
    {
      "source": "Example Foundry",
      "aliases": ["Example Foundry Ltd"],
      "replacement": "Juniper Works",
      "ignore_case": true
    }
  ]
}
```

Rules match whole tokens by default. Set `"match": "literal"` for a reviewed substring and `"scope": "content"` or `"scope": "path"` to limit a rule. Rules without a replacement use `MACHINA_PSEUDONYM_KEY` and an optional `category` to derive a repeatable pseudonym. Keep the key stable and private between runs.

The runner stages a fresh output directory and reports counts by status and extension. It edits STEP text fields, labels and paths in ASCII DXF, IGES header metadata, supported SAT and STL metadata, VRML text and URLs, SVG labels and metadata, and selectable PDF text and supported metadata. Unsupported, empty, and oversized files are copied and counted. An adapter failure aborts the run by default. Use `--preserve-on-error` on development runs to retain failed inputs for investigation.

ZIP and TAR processing recursively visits supported member files and reports leaf-file outcomes. It preserves member names, validates paths and limits expanded bytes and nesting, and rejects encrypted members and symbolic links. Supported TAR compression includes gzip, bzip2, and xz. Archive comments and ownership fields remain outside the current adapters.

Some archive members contain Git LFS pointers in place of the CAD data. The runner preserves those bytes and counts them as `source_pointer`. [Git LFS pointer specification](https://github.com/git-lfs/git-lfs/blob/main/docs/spec.md)

FreeCAD processing edits selected document metadata, supported external links, and referenced TechDraw SVG content. The native CFB adapter edits eligible OLE SummaryInformation and DocumentSummaryInformation properties in recognized SolidWorks and Inventor containers when replacements fit existing allocations. It leaves geometry and other streams intact. [FreeCAD format documentation](https://github.com/FreeCAD/FreeCAD-documentation/blob/main/wiki/File_Format_FCStd.md), [Microsoft OLE property set specification](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-oleps/)

Install `.[solidworks]` for typed metadata discovery and bounded metadata edits in modern SolidWorks files. Writes require a version with verified upstream write support. Internal version 12000, used by all 90 locally sampled modern files, is readable and retains its original bytes in production runs. [SWFormat](https://github.com/KenM76/swformat/tree/69b80c860241fdcbfa334f67cb951f76398c1a71)

PDF attachment edits cover eligible descriptions, filenames and supported immediate payloads. GoToE links, portfolios, and other protected relationships may restrict renaming. Nested PDFs and unsupported attachment payloads are preserved.

### DWG and DXF

ASCII DXF is edited in place by group code. The adapter rewrites visible text (TEXT, MTEXT, ATTRIB, ATTDEF, dimensions, leaders, tables), drawing properties in the header (`$AUTHOR`, `$LASTSAVEDBY`, `$TITLE`, custom properties, `$PROJECTNAME`, `$HYPERLINKBASE` and related fields), XRECORD, FIELD and DICTIONARYVAR strings, extended data, and xref, image and underlay paths. It removes the preview thumbnail. Every edit is checked so that only these string fields change: handles, `$HANDSEED`, numbers, and layer, block and layout names stay as they were. Binary DXF, embedded OLE and VBA payloads, and proxy objects are not edited.

DWG is opt-in with `--edit-dwg` and needs the free [ODA File Converter](https://www.opendesign.com/guestfiles/oda_file_converter). Each file is converted to DXF in its own version, rewritten as above, converted back to DWG in the same version, and checked again. The check covers the version and the absence of any text a rule would still replace. It also requires the drawing sections (entities, blocks and tables, numbers included) to match an unedited ODA round trip of the same file after the same edits. That baseline absorbs ODA's own normalisation: renumbered anonymous blocks, dropped default values, re-encoded proxy graphics and regenerated solid GUIDs. ODA's terms forbid redistributing the converter, so install it locally and never package it with the corpus or code.

ODA records the login name of the user running it as `$LASTSAVEDBY` on every write, including when it reads a DWG. Run it through `tools/oda-neutral.sh` so that outputs record `machina` instead. The adapter refuses output that carries your login name. Because ODA hides each source's original value, `--edit-dwg` rewrites every DWG. Rewritten DWGs lose the TrustedDWG marker, which AutoCAD reports as a notice.

ODA is a Qt program that needs an X display. Keep its window hidden with `xvfb`, or on hosts without root use `tools/hidden-xvfb.sh`.

```sh
tools/hidden-xvfb.sh :97
export ODA_FILE_CONVERTER=/path/to/ODAFileConverter MACHINA_ODA_DISPLAY=:97
export MACHINA_ODA_CONVERTER="$PWD/tools/oda-neutral.sh"
python -m machina_deid run input output/dev/corpus --rules private/rules.json --edit-dwg
```

`docs/autocad-probe.md` records the round-trip measurements behind these choices.

Path renaming is opt-in with `--rename-paths`. The runner plans the namespace before writing and updates references recognized by the adapters. Review relative and native CAD references before enabling it on a submission. Scanned PDF text, logos, and native references outside the supported adapters still need coverage.

## Package a local attempt

Run from the committed pipeline revision you want to submit. Save the aggregate report outside the attempt directory.

```sh
mkdir -p private
python -m machina_deid run input output/attempt-1/corpus --rules private/rules.json > private/run-report.json
python -m machina_deid prepare output/attempt-1 --report private/run-report.json --revision HEAD
```

The packager checks the corpus file count and byte total, copies allowlisted source files from the selected Git revision to `code/`, and writes `README.txt` with the run options and exception counts. Review the finished attempt and follow the official track's upload workflow. [Builders CAD De-ID track](https://www.joinbuilders.org/buildathon/tracks/machina-deid)

## Research

- The NIST STEP analyzer checks STEP syntax, PMI, and validation properties. Use it to compare representative files before and after an edit. [NIST STEP File Analyzer and Viewer](https://www.nist.gov/services-resources/software/step-file-analyzer-and-viewer)
- The SOLIDWORKS Document Manager can inspect properties and references. Inventor Apprentice can inspect properties and replace references in files that do not need migration. The tools require compatible file versions and, for Document Manager, a license key. [SOLIDWORKS Document Manager](https://help.solidworks.com/2026/english/api/swdocmgrapi/GettingStarted-swdocmgrapi.html?id=8.2), [Inventor Apprentice](https://help.autodesk.com/cloudhelp/2026/ENU/Inventor-API/files/Apprentice_Overview.htm), [Inventor ReplaceReference](https://help.autodesk.com/cloudhelp/2027/ENU/Inventor-API/files/FileDescriptor_ReplaceReference.htm)
- eDOCr2 targets tables, title blocks, and text regions. Autodesk's drawing extraction uses reusable title-block templates. Both approaches motivate grouping repeated layouts before running OCR. [eDOCr2 paper](https://www.mdpi.com/2075-1702/13/3/254), [Autodesk drawing extraction guide](https://help.autodesk.com/cloudhelp/ENU/Docs-Files/files/files-automated-drawing-extraction/Automated_Drawing_Extraction_Authoring_Guide.html)
- NVIDIA NeMo Retriever provides text and image extraction. Native CAD edits still need format adapters and validation. [NeMo Retriever documentation](https://docs.nvidia.com/nemo/retriever/latest/index.html)
