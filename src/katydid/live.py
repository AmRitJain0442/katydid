"""Bounded, read-only workflow snapshots from runner-owned evidence directories."""

import json
import os
import re
import stat
from itertools import islice
from pathlib import Path
from typing import Any

from katydid.store import TERMINAL_STATES

MAX_JSON_BYTES = 1024 * 1024
MAX_LOG_BYTES = 16 * 1024
MAX_RUNS = 16
MAX_OPEN_LOGS = 8
CHECK_ID = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
RUN_ID = re.compile(r"^[a-f0-9]{32}$")
TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def tool_name(check: dict[str, Any], repository: str = "") -> str:
    """Prefer explicit metadata; recognize direct CLIs and shipped legacy adapters."""
    explicit = check.get("tool")
    if isinstance(explicit, str) and explicit.strip():
        return explicit[:80]
    argv = check.get("argv")
    if not isinstance(argv, (list, tuple)) or not argv:
        return "Tool not specified"
    args = [str(arg).replace("\\", "/") for arg in argv]
    if "katydid.security" in args:
        index = args.index("katydid.security")
        if len(args) > index + 1:
            return {"static": "Semgrep", "dependencies": "Trivy", "secrets": "Gitleaks"}.get(
                args[index + 1], "Vultron security adapter"
            )
    if "katydid.deployment" in args:
        return "Vultron deployer"
    # Old Orders evidence predates tool metadata. Only recognize the exact shipped
    # profile and wrappers, rather than assuming every check named browser uses Playwright.
    if repository == "katydid-orders-lab":
        if len(args) > 2 and args[1] == "scripts/quality.py":
            return {
                "prepare": "uv + npm + Playwright",
                "cleanup": "Python",
                "unit": "pytest",
                "api": "pytest",
                "lint": "Ruff",
                "browser": "Playwright",
            }.get(args[2], "Python")
        if len(args) > 1 and args[1] == "scripts/run_isolated.py":
            return "Docker"
    commands = {
        "pytest": "pytest",
        "ruff": "Ruff",
        "playwright": "Playwright",
        "semgrep": "Semgrep",
        "trivy": "Trivy",
        "gitleaks": "Gitleaks",
        "docker": "Docker",
        "vitest": "Vitest",
        "jest": "Jest",
        "mypy": "mypy",
        "uv": "uv",
        "npm": "npm",
    }
    executable = args[0].rsplit("/", 1)[-1].removesuffix(".exe")
    candidate = executable
    if "-m" in args[:3]:
        index = args.index("-m")
        if len(args) > index + 1:
            candidate = args[index + 1]
    elif executable in {"npx", "npm", "uv", "uvx"}:
        candidate = next((arg for arg in args[1:4] if arg in commands), executable)
    if candidate in commands:
        return commands[candidate]
    if any("/node_modules/@playwright/test/cli." in arg for arg in args[:3]):
        return "Playwright"
    return {
        "{python}": "Python",
        "python": "Python",
        "python3": "Python",
        "node": "Node.js",
        "bash": "Bash",
        "pwsh": "PowerShell",
        "powershell": "PowerShell",
    }.get(executable, executable[:80])


def _safe_path(root: Path, path: Path) -> bool:
    """Do not follow links, Windows junctions/reparse points, or escape the state root."""
    try:
        parts = path.relative_to(root).parts
        if any(part in {".", ".."} for part in parts):
            return False
        current = root
        for part in parts:
            current /= part
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                return False
        return path.resolve().is_relative_to(root)
    except (OSError, ValueError):
        return False


def _read(root: Path, path: Path, limit: int, *, tail: bool = False) -> tuple[bytes, int]:
    if not _safe_path(root, path):
        raise OSError("Evidence is unavailable")
    before = path.stat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise OSError("Evidence must be an unlinked regular file")
    descriptor = os.open(
        path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    )
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (
            (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or opened.st_nlink != 1
            or not _safe_path(root, path)
        ):
            raise OSError("Evidence changed during read")
        if tail:
            stream.seek(max(0, opened.st_size - limit))
        elif opened.st_size > limit:
            raise OSError("Evidence exceeds the read budget")
        return stream.read(limit), opened.st_size


def _json(root: Path, path: Path) -> dict[str, Any]:
    try:
        value = json.loads(_read(root, path, MAX_JSON_BYTES)[0])
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, RecursionError):
        return {}


def _log(root: Path, path: Path) -> dict[str, Any]:
    try:
        raw, size = _read(root, path, MAX_LOG_BYTES, tail=True)
        # Strip terminal control sequences; text is still rendered only as DOM text.
        text = raw.decode("utf-8", "replace")
        text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
        text = "".join(char for char in text if char in "\n\r\t" or ord(char) >= 32)
        return {"text": text, "bytes": size, "truncated": size > MAX_LOG_BYTES, "available": True}
    except OSError:
        return {"text": "", "bytes": 0, "truncated": False, "available": False}


def _steps(root: Path, run: Path, snapshot: dict[str, Any], terminal: bool) -> list[dict[str, Any]]:
    plan = snapshot.get("plan", {})
    if not isinstance(plan, dict):
        return []
    rows: list[dict[str, Any]] = []

    def add(checks: Any, results: Any, phase: str) -> None:
        if not isinstance(checks, list):
            return
        results = results if isinstance(results, list) else []
        for index, check in enumerate(checks[:100]):
            if not isinstance(check, dict) or not CHECK_ID.fullmatch(str(check.get("id", ""))):
                continue
            name = check["id"]
            relative = f"{index:03d}-{name}"
            if phase != "check":
                relative = f"environment/{phase}-{relative}"
            folder = run / relative
            progress = _json(root, folder / "progress.json")
            result = next(
                (item for item in results if isinstance(item, dict) and item.get("id") == name), {}
            )
            value = result or progress
            status = value.get("status", "pending")
            if status == "running" and (terminal or snapshot.get("state") != "running"):
                status = "interrupted"
            if status == "pending" and (terminal or snapshot.get("state") != "running"):
                status = "not_run"
            rows.append(
                {
                    "key": f"{run.parent.name}/{run.name}/{relative}",
                    "name": name,
                    "tool": tool_name(check, str(plan.get("repository", ""))),
                    "kind": check.get("kind", "command"),
                    "phase": phase,
                    "status": status,
                    "started_at": progress.get("started_at"),
                    "finished_at": progress.get("finished_at"),
                    "duration_seconds": value.get("duration_seconds"),
                    "detail": value.get("detail", ""),
                    "evidence": value.get("evidence"),
                }
            )

    environment = plan.get("environment") or {}
    environment_result = snapshot.get("environment") or {}
    if not isinstance(environment, dict) or not isinstance(environment_result, dict):
        return []
    add(environment.get("prepare"), environment_result.get("prepare"), "prepare")
    readiness = environment.get("readiness")
    if isinstance(readiness, dict) and isinstance(readiness.get("check"), dict):
        probes = environment_result.get("readiness", [])
        if isinstance(probes, list):
            # Include the next probe while readiness is active. Each attempt has its own folder.
            count = len(probes) + (environment_result.get("phase") == "readiness")
            for index in range(min(count, 100)):
                check = readiness["check"]
                name = str(check.get("id", ""))
                if CHECK_ID.fullmatch(name):
                    progress = _json(
                        root, run / f"environment/readiness-{index:03d}-{name}" / "progress.json"
                    )
                    result = probes[index] if index < len(probes) else progress
                    if not isinstance(result, dict):
                        continue
                    status = result.get("status", "pending")
                    if status == "running" and terminal:
                        status = "interrupted"
                    rows.append(
                        {
                            "key": (
                                f"{run.parent.name}/{run.name}/"
                                f"environment/readiness-{index:03d}-{name}"
                            ),
                            "name": name,
                            "tool": tool_name(check, str(plan.get("repository", ""))),
                            "kind": check.get("kind", "command"),
                            "phase": "readiness",
                            "status": status,
                            "started_at": progress.get("started_at"),
                            "duration_seconds": result.get("duration_seconds"),
                            "detail": result.get("detail", ""),
                        }
                    )
    add(plan.get("checks"), snapshot.get("results"), "check")
    add(environment.get("cleanup"), environment_result.get("cleanup"), "cleanup")
    return rows


def workflow_snapshot(
    directory: Path, task: dict[str, Any], open_logs: list[str]
) -> dict[str, Any]:
    """Read only the known task's current epoch; log keys must match listed steps."""
    if len(open_logs) > MAX_OPEN_LOGS:
        raise ValueError("At most eight tool outputs can be expanded at once")
    task_id, epoch = task.get("id"), task.get("epoch", 0)
    if not isinstance(task_id, str) or not TASK_ID.fullmatch(task_id):
        raise ValueError("Invalid task identity")
    if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 0:
        raise ValueError("Invalid task epoch")
    root = directory.resolve()
    task_root = root / "tasks" / task_id / str(epoch)
    candidates: list[Path] = []
    truncated = False
    for category in ("runs", "release-runs"):
        parent = task_root / category
        if not _safe_path(root, parent):
            continue
        try:
            entries = list(islice(parent.iterdir(), 65))
            truncated |= len(entries) > 64
            candidates.extend(item for item in entries[:64] if RUN_ID.fullmatch(item.name))
        except OSError:
            continue
    snapshots = [(run, _json(root, run / "run.json")) for run in candidates]
    snapshots = [(run, data) for run, data in snapshots if data.get("run_id") == run.name]
    snapshots.sort(key=lambda pair: str(pair[1].get("started_at", "")))
    truncated |= len(snapshots) > MAX_RUNS
    runs = []
    log_paths: dict[str, Path] = {}
    for run, data in snapshots[-MAX_RUNS:]:
        steps = _steps(root, run, data, task.get("state") in TERMINAL_STATES)
        for step in steps:
            log_paths[step["key"]] = task_root / step["key"]
        runs.append(
            {
                "id": run.name,
                "category": run.parent.name,
                "stage": data.get("plan", {}).get("stage"),
                "state": data.get("state"),
                "started_at": data.get("started_at"),
                "updated_at": data.get("updated_at"),
                "gate": data.get("gate"),
                "environment_phase": (data.get("environment") or {}).get("phase"),
                "steps": steps,
            }
        )
    logs = {}
    for key in dict.fromkeys(open_logs):
        if key not in log_paths:
            continue  # A stale UI selection never becomes an arbitrary filesystem read.
        logs[key] = {
            name: _log(root, log_paths[key] / f"{name}.log") for name in ("stdout", "stderr")
        }
    return {
        "available": True,
        "task_id": task_id,
        "epoch": epoch,
        "state": task.get("state"),
        "runs": runs,
        "logs": logs,
        "truncated": truncated,
    }
