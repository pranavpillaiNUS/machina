"""Command line entry point for local corpus processing."""

import argparse
import json
import os
from pathlib import Path

from .discovery import inventory_local, inventory_manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m machina_deid")
    commands = parser.add_subparsers(dest="command", required=True)
    inventory = commands.add_parser("inventory", help="summarize the corpus or its manifest")
    inventory.add_argument("input", type=Path)
    inventory.add_argument("--manifest", action="store_true", help="read a mirror TSV manifest")
    run = commands.add_parser("run", help="rewrite a new output corpus")
    run.add_argument("input", type=Path)
    run.add_argument("output", type=Path)
    run.add_argument("--rules", type=Path, required=True, help="private JSON rules file")
    run.add_argument("--workers", type=int, default=4)
    run.add_argument("--max-edit-mib", type=int, default=64)
    run.add_argument("--rename-paths", action="store_true", help="apply planned path renames")
    run.add_argument("--preserve-on-error", action="store_true", help="copy adapter failures for review")
    args = parser.parse_args(argv)
    try:
        if args.command == "inventory":
            read_inventory = inventory_manifest if args.manifest else inventory_local
            result = read_inventory(args.input).as_dict()
        else:
            from .pipeline import RunOptions, rewrite_corpus
            from .policy import Rulebook

            rules = Rulebook.from_file(args.rules, key=os.environ.get("MACHINA_PSEUDONYM_KEY"))
            result = rewrite_corpus(
                args.input,
                args.output,
                rules,
                RunOptions(
                    workers=args.workers,
                    max_edit_bytes=args.max_edit_mib * 1024**2,
                    rename_paths=args.rename_paths,
                    preserve_on_error=args.preserve_on_error,
                ),
            )
        print(json.dumps(result, indent=2))
    except ValueError as error:
        parser.exit(2, f"Error: {error}\n")
    except OSError as error:
        parser.exit(2, f"Error: Cannot complete local file operation ({type(error).__name__}).\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
