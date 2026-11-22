# Machina CAD de-identification

This project builds a reproducible pipeline for the [Builders Buildathon CAD De-ID track](https://www.joinbuilders.org/buildathon/tracks/machina-deid). The goal is to replace identities consistently while preserving engineering data, file usability, and inter-file references.

## Get started

Use Python 3.11 or newer. Run commands from the repo root.

```sh
python -m machina_deid inventory input
```

The inventory reports file counts and sizes by extension without listing original paths. It excludes the organizer's `input/.get-input/` download metadata.

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

The initial CLI provides an inventory. Format adapters and reference validation are separate milestones.

## Research

- The NIST STEP analyzer checks STEP syntax, PMI, and validation properties. Use it to compare representative files before and after an edit. [NIST STEP File Analyzer and Viewer](https://www.nist.gov/services-resources/software/step-file-analyzer-and-viewer)
- The SOLIDWORKS Document Manager can inspect properties and references. Inventor Apprentice can inspect properties and replace references in files that do not need migration. The tools require compatible file versions and, for Document Manager, a license key. [SOLIDWORKS Document Manager](https://help.solidworks.com/2026/english/api/swdocmgrapi/GettingStarted-swdocmgrapi.html?id=8.2), [Inventor Apprentice](https://help.autodesk.com/cloudhelp/2026/ENU/Inventor-API/files/Apprentice_Overview.htm), [Inventor ReplaceReference](https://help.autodesk.com/cloudhelp/2027/ENU/Inventor-API/files/FileDescriptor_ReplaceReference.htm)
- eDOCr2 targets tables, title blocks, and text regions. Autodesk's drawing extraction uses reusable title-block templates. Both approaches motivate grouping repeated layouts before running OCR. [eDOCr2 paper](https://www.mdpi.com/2075-1702/13/3/254), [Autodesk drawing extraction guide](https://help.autodesk.com/cloudhelp/ENU/Docs-Files/files/files-automated-drawing-extraction/Automated_Drawing_Extraction_Authoring_Guide.html)
- NVIDIA NeMo Retriever provides text and image extraction. Native CAD edits still need format adapters and validation. [NeMo Retriever documentation](https://docs.nvidia.com/nemo/retriever/latest/index.html)
