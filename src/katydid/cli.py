"""Command-line interface; execution capabilities are added in independent slices."""

import argparse

from katydid import __version__


def main() -> int:
    parser = argparse.ArgumentParser(prog="katydid", description="Repository testing with evidence")
    parser.add_argument("--version", action="version", version=f"katydid {__version__}")
    parser.parse_args()
    parser.print_help()
    return 0
