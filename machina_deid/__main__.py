"""Command line entry point for local corpus processing."""

import argparse
import json
from pathlib import Path

from .discovery import InventoryError, inventory_local, inventory_manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m machina_deid")
    commands = parser.add_subparsers(dest="command", required=True)
    inventory = commands.add_parser("inventory", help="summarize the corpus or its manifest")
    inventory.add_argument("input", type=Path)
    inventory.add_argument("--manifest", action="store_true", help="read a mirror TSV manifest")
    args = parser.parse_args(argv)
    try:
        read_inventory = inventory_manifest if args.manifest else inventory_local
        print(json.dumps(read_inventory(args.input).as_dict(), indent=2))
    except InventoryError as error:
        parser.exit(2, f"Error: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
