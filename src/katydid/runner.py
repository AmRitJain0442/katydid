"""Trusted local execution with fresh evidence, timeouts, and cooperative cancellation.

This adapter is not a sandbox. Commands must remain foreground processes and await
their children; detached daemons need a future supervised environment adapter.
"""

import hashlib
import json
import os
import platform
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from katydid import __version__
from katydid.environment import EnvironmentResult, EnvironmentSession
from katydid.evidence import CheckResult, Gate, Status, evaluate_gate, read_junit
from katydid.profile import Plan, PlannedCheck, ProfileError

POLL_SECONDS = 0.05
MAX_LOG_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True)
class Run:
    id: str
    directory: Path
    results: tuple[CheckResult, ...]
    gate: Gate
    cancelled: bool
    environment: EnvironmentResult | None = None


def _write_json(path: Path, value: Any) -> None:
    """Replace a complete JSON checkpoint; never expose a half-written document."""
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".checkpoint-",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _git_identity(root: Path) -> dict[str, Any]:
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout.strip()
        return {"head": head, "dirty": bool(dirty)}
    except (OSError, subprocess.SubprocessError):
        return {"head": None, "dirty": None}


def _stop_process(process: subprocess.Popen[bytes]) -> str:
    """Bound termination; return a visible cleanup diagnostic if it fails."""
    try:
        if sys.platform == "win32":
            if process.poll() is None:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    check=True,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        return ""
    except (OSError, subprocess.SubprocessError) as exc:
        try:
            process.kill()
            process.wait(timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass
        return f"; process-tree cleanup could not be confirmed: {exc}"


def _execute(
    check: PlannedCheck,
    plan: Plan,
    folder: Path,
    run_id: str,
    cancel: threading.Event,
    cancel_file: Path,
    environment_directory: Path | None = None,
) -> CheckResult:
    started = time.monotonic()
    report = folder / "junit.xml"
    stdout = folder / "stdout.log"
    stderr = folder / "stderr.log"
    argv = []
    for arg in check.argv:
        if arg == "{python}":
            arg = sys.executable
        else:
            arg = arg.replace("{report}", str(report)).replace("{run_id}", run_id)
            if environment_directory is not None:
                arg = arg.replace("{environment}", str(environment_directory))
        argv.append(arg)
    cwd = Path(check.working_directory).resolve()
    if not cwd.is_relative_to(Path(plan.root)) or not cwd.is_dir():
        return CheckResult(check.id, Status.ERROR, "Working directory changed or escaped the root")
    environment = os.environ.copy()
    if environment_directory is not None:
        environment["KATYDID_ENVIRONMENT_DIR"] = str(environment_directory)
    else:
        environment.pop("KATYDID_ENVIRONMENT_DIR", None)
    environment.update(
        KATYDID_RUN_ID=run_id,
        KATYDID_REPORT_PATH=str(report),
        PYTHONUNBUFFERED="1",
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONPYCACHEPREFIX=str(folder / "pycache"),
    )
    _write_json(
        folder / "invocation.json",
        {"argv": argv, "cwd": str(cwd), "timeout_seconds": check.timeout_seconds},
    )
    process: subprocess.Popen[bytes] | None = None
    status: Status | None = None
    detail = ""
    try:
        with stdout.open("wb") as out, stderr.open("wb") as err:
            if cancel.is_set() or cancel_file.exists():
                return CheckResult(check.id, Status.CANCELLED, "Cancelled before process launch")
            options: dict[str, Any] = {}
            if sys.platform == "win32":
                options["creationflags"] = (
                    subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
                )
            else:
                options["start_new_session"] = True
            process = subprocess.Popen(
                argv,
                cwd=cwd,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                shell=False,
                **options,
            )
            while True:
                if cancel.is_set() or cancel_file.exists():
                    status, detail = Status.CANCELLED, "Cancellation requested"
                    break
                if stdout.stat().st_size + stderr.stat().st_size > MAX_LOG_BYTES:
                    status, detail = Status.ERROR, "Combined process logs exceeded 10 MiB"
                    break
                if process.poll() is not None:
                    break
                if time.monotonic() - started >= check.timeout_seconds:
                    status, detail = Status.TIMED_OUT, "Process exceeded its configured timeout"
                    break
                time.sleep(POLL_SECONDS)
            if status is not None:
                detail += _stop_process(process)
            else:
                process.wait(timeout=5)
    except KeyboardInterrupt:
        cancel.set()
        status, detail = Status.CANCELLED, "Interrupted"
        if process is not None:
            detail += _stop_process(process)
    except (OSError, subprocess.SubprocessError) as exc:
        status, detail = Status.ERROR, f"Cannot execute check: {exc}"
        if process is not None:
            detail += _stop_process(process)
    elapsed = round(time.monotonic() - started, 6)
    exit_code = process.returncode if process is not None else None
    evidence = read_junit(report) if check.kind == "test" else None
    if status is None:
        if exit_code != 0:
            status, detail = Status.FAILED, f"Process exited with code {exit_code}"
        elif evidence is not None:
            status, detail = evidence.status, evidence.detail
        else:
            status, detail = Status.PASSED, "Command exited successfully"
    return CheckResult(check.id, status, detail, exit_code, elapsed, evidence)


def run_plan(
    plan: Plan,
    output: Path | None = None,
    cancel: threading.Event | None = None,
    on_start: Callable[[Path], None] | None = None,
) -> Run:
    cancel = cancel if cancel is not None else threading.Event()
    raw_profile = Path(plan.profile_path).read_bytes()
    if hashlib.sha256(raw_profile).hexdigest() != plan.profile_sha256:
        raise ProfileError("Profile changed after planning; generate a fresh plan")
    run_id = uuid.uuid4().hex
    parent = (output or Path(plan.root) / ".katydid" / "runs").resolve()
    directory = parent / run_id
    directory.mkdir(parents=True, exist_ok=False)
    cancel_file = directory / "cancel.request"
    results: list[CheckResult] = []
    session: EnvironmentSession | None = None
    execution_error: str | None = None
    metadata = {
        "schema_version": 1,
        "run_id": run_id,
        "katydid_version": __version__,
        "started_at": datetime.now(UTC).isoformat(),
        "python": sys.version,
        "platform": platform.platform(),
        "git": _git_identity(Path(plan.root)),
        "plan": plan.to_dict(),
    }
    (directory / "profile.yaml").write_bytes(raw_profile)
    _write_json(directory / "plan.json", plan.to_dict())

    def checkpoint(state: str) -> Gate:
        gate = evaluate_gate(plan, results)
        reasons = list(gate.reasons)
        if session is not None:
            if not session.ready:
                reasons.append("Environment preparation/readiness did not succeed")
            if not session.cleanup_complete:
                reasons.append("Environment cleanup is incomplete or failed")
        if execution_error is not None:
            reasons.append(execution_error)
        gate = Gate(not reasons, tuple(reasons), gate.advisories)
        if state != "completed":
            gate = Gate(False, (*gate.reasons, f"Run is {state}"), gate.advisories)
        _write_json(
            directory / "run.json",
            {
                **metadata,
                "state": state,
                "updated_at": datetime.now(UTC).isoformat(),
                "results": [asdict(result) for result in results],
                "gate": asdict(gate),
                "environment": asdict(session.snapshot()) if session else None,
                "execution_error": execution_error,
            },
        )
        return gate

    if plan.environment is not None:
        environment_directory = directory / "environment" / "data"

        def lifecycle_check(
            check: PlannedCheck, folder: Path, interrupted: threading.Event, marker: Path
        ) -> CheckResult:
            return _execute(check, plan, folder, run_id, interrupted, marker, environment_directory)

        def lifecycle_checkpoint() -> None:
            checkpoint("running")

        session = EnvironmentSession(
            plan.environment,
            directory / "environment",
            lifecycle_check,
            _write_json,
            lifecycle_checkpoint,
        )
    try:
        checkpoint("running")
        if on_start is not None:
            on_start(directory)
        ready = session.prepare(cancel, cancel_file) if session else True
        for index, check in enumerate(plan.checks):
            if cancel.is_set() or cancel_file.exists():
                cancel.set()
                results.append(
                    CheckResult(check.id, Status.CANCELLED, "Cancelled before execution")
                )
            elif not ready:
                results.append(
                    CheckResult(
                        check.id,
                        Status.ERROR,
                        "Environment preparation/readiness failed; check not executed",
                    )
                )
            else:
                folder = directory / f"{index:03d}-{check.id}"
                folder.mkdir()
                result = _execute(
                    check,
                    plan,
                    folder,
                    run_id,
                    cancel,
                    cancel_file,
                    session.data if session else None,
                )
                results.append(result)
                if result.status == Status.CANCELLED:
                    cancel.set()
            checkpoint("running")
    except BaseException as exc:
        execution_error = f"Execution aborted: {type(exc).__name__}: {exc}"
        if session is not None:
            session.error = execution_error
        raise
    finally:
        try:
            if session is not None:
                session.cleanup()
        except BaseException as exc:
            cleanup_error = f"Cleanup aborted: {type(exc).__name__}: {exc}"
            execution_error = (
                f"{execution_error}; {cleanup_error}" if execution_error else cleanup_error
            )
            raise
        finally:
            cancelled = cancel.is_set() or cancel_file.exists()
            gate = checkpoint("cancelled" if cancelled else "completed")
    return Run(
        run_id, directory, tuple(results), gate, cancelled, session.snapshot() if session else None
    )
