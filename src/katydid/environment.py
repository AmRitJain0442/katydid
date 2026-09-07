"""Ordered local setup, bounded readiness, and cancellation-independent cleanup."""

import math
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from katydid.evidence import CheckResult, Status
from katydid.profile import PlannedCheck, PlannedEnvironment

Execute = Callable[[PlannedCheck, Path, threading.Event, Path], CheckResult]


@dataclass(frozen=True)
class EnvironmentResult:
    directory: str
    phase: str
    started: bool
    ready: bool
    cleanup_complete: bool
    prepare: tuple[CheckResult, ...]
    readiness: tuple[CheckResult, ...]
    cleanup: tuple[CheckResult, ...]
    error: str | None


class EnvironmentSession:
    def __init__(
        self,
        plan: PlannedEnvironment,
        directory: Path,
        execute: Execute,
        write_json: Callable[[Path, Any], None],
        on_change: Callable[[], None],
    ) -> None:
        self.plan = plan
        self.directory = directory
        self.data = directory / "data"
        self.data.mkdir(parents=True)
        self.execute = execute
        self.write_json = write_json
        self.on_change = on_change
        self.phase = "pending"
        self.started = False
        self.ready = False
        self.cleanup_complete = False
        self.prepared: list[CheckResult] = []
        self.probes: list[CheckResult] = []
        self.cleaned: list[CheckResult] = []
        self.error: str | None = None

    def snapshot(self) -> EnvironmentResult:
        return EnvironmentResult(
            str(self.data),
            self.phase,
            self.started,
            self.ready,
            self.cleanup_complete,
            tuple(self.prepared),
            tuple(self.probes),
            tuple(self.cleaned),
            self.error,
        )

    def _changed(self) -> None:
        self.write_json(self.directory / "environment.json", asdict(self.snapshot()))
        self.on_change()

    def _invoke(
        self,
        phase: str,
        index: int,
        check: PlannedCheck,
        cancel: threading.Event,
        cancel_file: Path,
    ) -> CheckResult:
        folder = self.directory / f"{phase}-{index:03d}-{check.id}"
        folder.mkdir()
        return self.execute(check, folder, cancel, cancel_file)

    def prepare(self, cancel: threading.Event, cancel_file: Path) -> bool:
        if cancel.is_set() or cancel_file.exists():
            cancel.set()
            self.phase = "cancelled"
            self._changed()
            return False
        self.phase = "preparing"
        self.started = True
        self._changed()  # Persist before the first operation can allocate an external resource.
        for index, check in enumerate(self.plan.prepare):
            result = self._invoke("prepare", index, check, cancel, cancel_file)
            self.prepared.append(result)
            self._changed()
            if result.status != Status.PASSED:
                return False
        readiness = self.plan.readiness
        if readiness is not None:
            self.phase = "readiness"
            self._changed()
            deadline = time.monotonic() + readiness.timeout_seconds
            while time.monotonic() < deadline and len(self.probes) < readiness.max_attempts:
                if cancel.is_set() or cancel_file.exists():
                    cancel.set()
                    return False
                remaining = max(1, math.ceil(deadline - time.monotonic()))
                check = replace(
                    readiness.check, timeout_seconds=min(remaining, readiness.check.timeout_seconds)
                )
                result = self._invoke("readiness", len(self.probes), check, cancel, cancel_file)
                self.probes.append(result)
                self._changed()
                if result.status == Status.PASSED:
                    if time.monotonic() > deadline:
                        self.error = (
                            "Readiness deadline expired before the successful probe completed"
                        )
                        return False
                    break
                if result.status in (Status.CANCELLED, Status.ERROR):
                    return False
                # File-based cancellation must be responsive during the retry delay too.
                retry_at = min(deadline, time.monotonic() + readiness.interval_seconds)
                while time.monotonic() < retry_at:
                    if cancel.wait(min(0.05, max(0, retry_at - time.monotonic()))):
                        return False
                    if cancel_file.exists():
                        cancel.set()
                        return False
            else:
                self.error = (
                    "Readiness attempt budget exhausted"
                    if len(self.probes) >= readiness.max_attempts
                    else "Readiness deadline expired"
                )
                return False
        if cancel.is_set() or cancel_file.exists():
            cancel.set()
            return False
        self.ready = True
        self.phase = "ready"
        self._changed()
        return True

    def cleanup(self) -> None:
        if not self.started:
            self.cleanup_complete = True
            self._changed()
            return
        self.phase = "cleaning"
        checkpoint_error: Exception | None = None

        def persist() -> None:
            nonlocal checkpoint_error
            try:
                self._changed()
            except Exception as exc:
                # Evidence storage failure must not prevent disposal of allocated resources.
                checkpoint_error = exc
                self.error = f"Cleanup checkpoint failed: {type(exc).__name__}: {exc}"

        persist()
        for index, check in enumerate(self.plan.cleanup):
            try:
                result = self._invoke(
                    "cleanup",
                    index,
                    check,
                    threading.Event(),
                    self.directory / "cleanup.cancel.request",
                )
            except Exception as exc:
                result = CheckResult(
                    check.id,
                    Status.ERROR,
                    f"Cleanup could not execute: {type(exc).__name__}: {exc}",
                )
            self.cleaned.append(result)
            persist()
        self.cleanup_complete = all(result.status == Status.PASSED for result in self.cleaned)
        self.phase = "finished"
        persist()
        if checkpoint_error is not None:
            raise checkpoint_error
