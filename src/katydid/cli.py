"""Command-line interface for profile validation and deterministic planning."""

import argparse
import json
import re
import signal
import subprocess
import sys
import threading
from pathlib import Path

from katydid import __version__
from katydid.profile import ProfileError, make_plan
from katydid.runner import run_plan
from katydid.service import add_commands, handle


def _sweep(namespace: str, watch: bool, interval: int) -> int:
    from katydid.docker import sweep

    if re.fullmatch(r"[a-z][a-z0-9-]{0,63}", namespace) is None or not 1 <= interval <= 3600:
        raise ValueError("Sweep needs a valid namespace and an interval from 1 to 3600 seconds")
    stop = threading.Event()
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    for sig in previous:
        signal.signal(sig, lambda _sig, _frame: stop.set())
    try:
        while not stop.is_set():
            try:
                result = sweep(namespace)
            except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                result = {"removed": [], "skipped": [], "errors": [str(exc)]}
            print(json.dumps(result), flush=True)
            if not watch:
                return 1 if result["errors"] else 0
            stop.wait(interval)
        return 0
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


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
    sweep_command = subparsers.add_parser("sweep", help="Remove expired owned Docker containers")
    sweep_command.add_argument("--namespace", required=True)
    sweep_command.add_argument("--watch", action="store_true")
    sweep_command.add_argument("--interval", type=int, default=30)
    add_commands(subparsers)
    args = parser.parse_args(argv)
    try:
        if args.command == "sweep":
            return _sweep(args.namespace, args.watch, args.interval)
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
