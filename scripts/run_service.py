"""Host entry point for an OS-supervised Vultron worker with bounded local logs."""

import argparse
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fleet", required=True, type=Path)
    parser.add_argument("--logs", required=True, type=Path)
    parser.add_argument("--credential-file", type=Path)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--schedule-seconds", type=int, default=86400)
    args = parser.parse_args()
    if not args.fleet.is_file() or args.interval < 1 or args.schedule_seconds < 0:
        parser.error("A valid fleet file and nonnegative schedule are required")
    environment = dict(os.environ, PYTHONUNBUFFERED="1")
    if args.credential_file:
        if not args.credential_file.is_file():
            parser.error("Configured application credential file is unavailable")
        environment["GOOGLE_APPLICATION_CREDENTIALS"] = str(args.credential_file.resolve())
    args.logs.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        args.logs / "service.log", maxBytes=5_000_000, backupCount=4, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logger = logging.getLogger("vultron-host")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    interpreter = Path(sys.executable)
    if interpreter.name.lower() == "pythonw.exe":
        interpreter = interpreter.with_name("python.exe")
    argv = [
        str(interpreter),
        "-m",
        "katydid",
        "serve",
        "--fleet",
        str(args.fleet.resolve()),
        "--host",
        "127.0.0.1",
        "--port",
        str(args.port),
        "--watch",
        "--interval",
        str(args.interval),
        "--schedule-seconds",
        str(args.schedule_seconds),
    ]
    stopping = threading.Event()
    child = None

    def terminate_owned_process() -> None:
        if child is None or child.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(child.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        else:
            child.terminate()

    def stop(_signal: int, _frame: object) -> None:
        stopping.set()
        terminate_owned_process()

    for name in (signal.SIGINT, signal.SIGTERM):
        signal.signal(name, stop)
    failures = 0
    try:
        while not stopping.is_set():
            started = time.monotonic()
            child = subprocess.Popen(
                argv,
                cwd=ROOT,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                **options,
            )
            logger.info("Service process started")
            assert child.stdout is not None
            while line := child.stdout.readline(8192):
                logger.info("%s", line.rstrip()[:8000])
            result = child.wait()
            child.stdout.close()
            logger.info("Service process stopped with exit code %s", result)
            if stopping.is_set():
                return 0
            failures = 1 if time.monotonic() - started >= 60 else failures + 1
            if failures >= 10:
                logger.error("Repeated startup failures; host supervisor intervention required")
                return 1
            delay = min(5 * failures, 60)
            logger.info("Restarting worker after %s seconds", delay)
            stopping.wait(delay)
        return 0
    finally:
        terminate_owned_process()
        if child is not None and child.poll() is None:
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        handler.close()


if __name__ == "__main__":
    raise SystemExit(main())
