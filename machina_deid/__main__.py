"""Command line entry point for local corpus processing."""

import argparse
import json
from pathlib import Path

from .corpus import scan, summarize


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m machina_deid")
    commands = parser.add_subparsers(dest="command", required=True)
    inventory = commands.add_parser("inventory", help="summarize local corpus files")
    inventory.add_argument("input", type=Path)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(summarize(scan(args.input)), indent=2))
    except (OSError, ValueError) as error:
        parser.exit(2, f"Error: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
