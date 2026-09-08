"""CLI and localhost service entry points for fleet automation."""

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from katydid.ai import codex_command
from katydid.controller import Controller
from katydid.dashboard import make_server
from katydid.fleet import load_fleet


def add_commands(subparsers: Any) -> None:
    onboarding = subparsers.add_parser(
        "onboard", help="Inspect a repository and prepare registration"
    )
    onboarding.add_argument("source")
    onboarding.add_argument("destination", type=Path)
    onboarding.add_argument("--repository-id")
    onboarding.add_argument("--owner", default="unassigned")
    onboarding.add_argument("--base-branch")
    fleet = subparsers.add_parser("fleet", help="Validate the central repository registry")
    fleet.add_argument("file", type=Path)
    doctor = subparsers.add_parser("doctor", help="Check local tools and AI authentication")
    doctor.add_argument("--fleet", type=Path, required=True)
    task = subparsers.add_parser("task", help="Submit, inspect, or interrupt durable tasks")
    task.add_argument("--fleet", type=Path, required=True)
    actions = task.add_subparsers(dest="action", required=True)
    submit = actions.add_parser("submit")
    submit.add_argument("repository")
    submit.add_argument("--key")
    submit.add_argument(
        "--stage", choices=("pull-request", "merge", "nightly", "release"), default="pull-request"
    )
    submit.add_argument("--mode", choices=("check", "repair", "release"), default="repair")
    actions.add_parser("list")
    for name in ("show", "events", "pause", "resume", "cancel", "steer"):
        action = actions.add_parser(name)
        action.add_argument("id")
        if name == "steer":
            action.add_argument("instruction")
    for name in ("worker", "serve"):
        parser = subparsers.add_parser(name)
        parser.add_argument("--fleet", type=Path, required=True)
        parser.add_argument(
            "--watch", action="store_true", help="Discover registered branch changes"
        )
        parser.add_argument("--interval", type=int, default=60)
        parser.add_argument(
            "--schedule-seconds",
            type=int,
            default=0,
            help="Also recheck unchanged heads on this schedule; zero disables",
        )
        if name == "worker":
            parser.add_argument("--once", action="store_true")
        else:
            parser.add_argument("--host", default="127.0.0.1")
            parser.add_argument("--port", type=int, default=8765)
    webhook = subparsers.add_parser("webhook", help="Receive signed GitHub events on loopback")
    webhook.add_argument("--fleet", type=Path, required=True)
    webhook.add_argument("--host", default="127.0.0.1")
    webhook.add_argument("--port", type=int, default=8766)
    webhook.add_argument("--secret-env", default="KATYDID_WEBHOOK_SECRET")
    demo = subparsers.add_parser("demo", help="Create or run the reproducible local fleet demo")
    demo.add_argument("action", choices=("init", "run"))
    demo.add_argument("directory", type=Path)
    demo.add_argument("--live-ai", action="store_true", help="Run real authenticated model calls")


def _print(value: Any) -> None:
    print(json.dumps(value, indent=2))


def doctor(fleet: Path) -> int:
    config, _digest = load_fleet(fleet)
    checks: dict[str, Any] = {"python": sys.version.split()[0]}
    passed = True
    commands = [("git", ["git", "--version"])]
    if config.ai.provider == "codex":
        commands.extend(
            [
                ("codex", [*codex_command(config.ai), "--version"]),
                ("ai_auth", [*codex_command(config.ai), "login", "status"]),
            ]
        )
    else:
        from katydid.gemini import gemini_doctor

        with tempfile.TemporaryDirectory(prefix="katydid-gemini-doctor-") as directory:
            checks["ai_auth"] = gemini_doctor(config.ai, Path(directory))
        passed &= checks["ai_auth"]["available"] is True
    for name, argv in commands:
        try:
            result = subprocess.run(argv, capture_output=True, text=True, timeout=20, check=False)
            checks[name] = {
                "available": result.returncode == 0,
                "detail": (result.stdout + result.stderr).strip()[:1000],
            }
            passed &= result.returncode == 0
        except (OSError, subprocess.SubprocessError) as exc:
            checks[name] = {"available": False, "detail": str(exc)}
            passed = False
    if any(repo.delivery.mode == "github" for repo in config.repositories):
        if not shutil.which("gh"):
            checks["github_auth"] = {"available": False}
            passed = False
        else:
            result = subprocess.run(
                ["gh", "auth", "status"], capture_output=True, text=True, timeout=20
            )
            # gh auth status can include a masked token; only expose the exit status.
            checks["github_auth"] = {"available": result.returncode == 0}
            passed &= result.returncode == 0
    _print({"passed": passed, "checks": checks})
    return 0 if passed else 1


def handle(args: argparse.Namespace) -> int:
    value: Any
    result: Any
    if args.command == "onboard":
        from katydid.onboarding import onboard

        result = onboard(
            args.source,
            args.destination,
            repository_id=args.repository_id,
            owner=args.owner,
            base_branch=args.base_branch,
        )
        _print(result.to_dict())
        return 0
    if args.command == "fleet":
        config, digest = load_fleet(args.file)
        _print(
            {
                "valid": True,
                "sha256": digest,
                "repositories": [r.id for r in config.repositories],
                "state_directory": config.state_directory,
            }
        )
        return 0
    if args.command == "doctor":
        return doctor(args.fleet)
    if args.command == "demo":
        from katydid.demo import initialize, run_demo

        if args.action == "init":
            _print({"fleet": str(initialize(args.directory))})
            return 0
        if not args.live_ai:
            raise ValueError(
                "Demo run requires --live-ai; deterministic fixtures run through pytest"
            )
        result = run_demo(args.directory)
        _print(result)
        return 0 if result["passed"] else 1
    controller = Controller(args.fleet)
    if args.command == "task":
        if args.action == "submit":
            value = controller.enqueue(args.repository, args.key, stage=args.stage, mode=args.mode)
        elif args.action == "list":
            value = controller.store.list_tasks()
        elif args.action == "show":
            value = controller.store.get_task(args.id)
        elif args.action == "events":
            value = controller.store.events(args.id)
        else:
            value = controller.store.control(
                args.id, args.action, getattr(args, "instruction", None)
            )
        _print(value)
        return 0
    if args.command == "webhook":
        from katydid.events import GitHubIngress
        from katydid.webhook import make_webhook_server

        secret = os.environ.get(args.secret_env, "").encode("utf-8")
        if len(secret) < 32:
            raise ValueError("Webhook secret environment variable must contain at least 32 bytes")
        server = make_webhook_server(GitHubIngress(controller), secret, args.host, args.port)
        stopping = threading.Event()
        handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        for sig in handlers:
            signal.signal(sig, lambda _sig, _frame: stopping.set())
        server.timeout = 0.25
        print(
            f"GitHub webhook listener: http://{args.host}:{server.server_port}/webhooks/github",
            flush=True,
        )
        try:
            while not stopping.is_set():
                server.handle_request()
        finally:
            server.server_close()
            for sig, handler in handlers.items():
                signal.signal(sig, handler)
        return 0
    if args.interval < 1 or args.schedule_seconds < 0:
        raise ValueError("interval must be positive and schedule-seconds must be nonnegative")
    if args.schedule_seconds and not args.watch:
        raise ValueError("Scheduled discovery requires --watch")
    stop = threading.Event()
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    for sig in previous:
        signal.signal(sig, lambda _sig, _frame: stop.set())
    try:
        if args.command == "worker" and args.once:
            if args.watch:
                controller.discover()
                if args.schedule_seconds:
                    controller.discover(period=int(time.time() // args.schedule_seconds))
            result = controller.work_once(stop)
            _print(result)
            return 0 if result is None or result["state"] == "completed" else 1

        def work() -> None:
            discovered = 0.0
            while not stop.is_set():
                try:
                    if args.watch and time.monotonic() - discovered >= args.interval:
                        period = (
                            int(time.time() // args.schedule_seconds)
                            if args.schedule_seconds
                            else None
                        )
                        controller.discover()
                        if period is not None:
                            controller.discover(period=period)
                        discovered = time.monotonic()
                    result = controller.work_once(stop)
                    if result is not None:
                        print(
                            f"Task {result['id']}: {result['state']}", file=sys.stderr, flush=True
                        )
                    else:
                        stop.wait(0.5)
                except Exception as exc:
                    print(f"Worker error: {exc}", file=sys.stderr, flush=True)
                    stop.wait(min(args.interval, 5))

        if args.command == "worker":
            work()
            return 0

        def runtime_status() -> dict[str, Any]:
            return {
                "managed": True,
                "worker_alive": thread.is_alive(),
                "provider": controller.config.ai.provider,
                "model": controller.config.ai.model,
                "watch": args.watch,
                "interval_seconds": args.interval,
                "schedule_seconds": args.schedule_seconds,
                "repositories": [
                    {
                        "id": repo.id,
                        "profile": repo.profile,
                        "required_checks": repo.required_checks,
                        "required_checks_by_stage": repo.required_checks_by_stage,
                        "delivery": repo.delivery.mode,
                        "auto_merge": repo.delivery.auto_merge,
                        "release_configured": repo.release is not None,
                        "auto_deploy": bool(repo.release and repo.release.auto_deploy),
                        "isolation_required": repo.isolation_policy is not None
                        and repo.isolation_policy.required,
                    }
                    for repo in controller.config.repositories
                ],
            }

        server = make_server(
            controller.store,
            [repo.id for repo in controller.config.repositories],
            controller.enqueue,
            args.host,
            args.port,
            runtime=runtime_status,
        )
        server.timeout = 0.25
        thread = threading.Thread(target=work, daemon=True, name="katydid-worker")
        thread.start()
        print(f"Vultron dashboard: http://{args.host}:{server.server_port}", flush=True)
        try:
            while not stop.is_set():
                server.handle_request()
        finally:
            server.server_close()
            stop.set()
            # A graceful shutdown must not abandon configured environment cleanup.
            thread.join()
        return 0
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
