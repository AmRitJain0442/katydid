"""Command-line interface for profile validation and deterministic planning."""

import argparse
import sys
from pathlib import Path

from katydid import __version__
from katydid.profile import ProfileError, make_plan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="katydid", description="Repository testing with evidence")
    parser.add_argument("--version", action="version", version=f"katydid {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "plan"):
        command = subparsers.add_parser(name)
        command.add_argument("profile", type=Path)
        command.add_argument("--root", type=Path)
        command.add_argument(
            "--stage", choices=("pull-request", "merge", "nightly"), default="pull-request"
        )
    args = parser.parse_args(argv)
    try:
        plan = make_plan(args.profile, args.stage, args.root)
    except ProfileError as exc:
        print(f"katydid: {exc}", file=sys.stderr)
        return 2
    if args.command == "plan":
        print(plan.to_json())
    else:
        print(f"Valid: {plan.repository}; {len(plan.checks)} selected check(s)")
    return 0
