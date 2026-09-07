"""Command-line interface for profile validation and deterministic planning."""

import argparse
import json
import signal
import sys
import threading
from pathlib import Path

from katydid import __version__
from katydid.profile import ProfileError, make_plan
from katydid.runner import run_plan
from katydid.service import add_commands, handle


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="katydid", description="Repository testing with evidence")
    parser.add_argument("--version", action="version", version=f"katydid {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "plan", "run"):
        command = subparsers.add_parser(name)
        command.add_argument("profile", type=Path)
        command.add_argument("--root", type=Path)
        command.add_argument(
            "--stage", choices=("pull-request", "merge", "nightly"), default="pull-request"
        )
        if name == "run":
            command.add_argument("--output", type=Path, help="Parent directory for unique runs")
    cancel_command = subparsers.add_parser("cancel", help="Request cancellation of a local run")
    cancel_command.add_argument("run_directory", type=Path)
    add_commands(subparsers)
    args = parser.parse_args(argv)
    try:
        if args.command in ("fleet", "doctor", "task", "worker", "serve", "demo"):
            return handle(args)
        if args.command == "cancel":
            summary = args.run_directory / "run.json"
            data = json.loads(summary.read_text(encoding="utf-8"))
            if (
                not isinstance(data, dict)
                or data.get("schema_version") != 1
                or data.get("run_id") != args.run_directory.resolve().name
                or data.get("state") not in ("running", "completed", "cancelled")
            ):
                raise ValueError("Not a recognized Katydid run checkpoint")
            if data.get("state") != "running":
                print("Run is already terminal; no cancellation requested")
                return 0
            (args.run_directory / "cancel.request").touch(exist_ok=True)
            print("Cancellation requested; inspect run.json for completion")
            return 0
        plan = make_plan(args.profile, args.stage, args.root)
        if args.command == "run":
            cancelled = threading.Event()
            previous = {
                signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)
            }
            for signum in previous:
                signal.signal(signum, lambda _signum, _frame: cancelled.set())
            try:
                run = run_plan(
                    plan,
                    args.output,
                    cancelled,
                    on_start=lambda path: print(f"Run evidence: {path}", file=sys.stderr),
                )
            finally:
                for signum, handler in previous.items():
                    signal.signal(signum, handler)
            print(
                json.dumps(
                    {
                        "run_id": run.id,
                        "directory": str(run.directory),
                        "passed": run.gate.passed,
                        "cancelled": run.cancelled,
                        "reasons": run.gate.reasons,
                        "advisories": run.gate.advisories,
                    },
                    indent=2,
                )
            )
            return 130 if run.cancelled else (0 if run.gate.passed else 1)
    except (ProfileError, OSError, ValueError, RuntimeError, KeyError) as exc:
        print(f"katydid: {exc}", file=sys.stderr)
        return 2
    if args.command == "plan":
        print(plan.to_json())
    else:
        print(f"Valid: {plan.repository}; {len(plan.checks)} selected check(s)")
    return 0
