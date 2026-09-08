"""Strict Docker execution and namespace-scoped abandoned-container cleanup.

This module deliberately uses only the Docker CLI.  It never pulls, builds, or
falls back to host execution, and it removes containers only after rechecking
their immutable ID and Katydid ownership labels.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, BinaryIO

from katydid.evidence import MAX_REPORT_BYTES, CheckResult, Status, TestEvidence, read_junit
from katydid.profile import Plan, PlannedCheck, PlannedIsolation, ProfileError, isolated_source

MANAGED_LABEL = "io.katydid.managed"
NAMESPACE_LABEL = "io.katydid.namespace"
RUN_LABEL = "io.katydid.run_id"
EXPIRY_LABEL = "io.katydid.expires_at"
MANAGED_VALUE = "true"

SOURCE_LIMIT_BYTES = 64 * 1024 * 1024
MAX_LOG_BYTES = 10 * 1024 * 1024
POLL_SECONDS = 0.05
DOCKER_TIMEOUT_SECONDS = 30

_CONTAINER_ID = re.compile(r"^[a-f0-9]{64}$")
_IMAGE_ID = re.compile(r"^sha256:[a-f0-9]{64}$")
_NAMESPACE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_RUN_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class DockerError(RuntimeError):
    """The requested isolation boundary could not be established or disposed."""


def _now() -> float:
    """Clock boundary kept patchable for deterministic crash-recovery acceptance tests."""
    return time.time()


def _docker_run(
    args: list[str], timeout: float = DOCKER_TIMEOUT_SECONDS
) -> subprocess.CompletedProcess[str]:
    environment = None
    if args[:1] == ["--host"]:
        environment = os.environ.copy()
        environment.pop("DOCKER_HOST", None)
        environment.pop("DOCKER_CONTEXT", None)
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        shell=False,
        env=environment,
    )


def _docker_start(container_id: str, host: str) -> subprocess.Popen[bytes]:
    options: dict[str, Any] = {}
    if sys.platform == "win32":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    environment = os.environ.copy()
    environment.pop("DOCKER_HOST", None)
    environment.pop("DOCKER_CONTEXT", None)
    return subprocess.Popen(
        ["docker", "--host", host, "start", "--attach", container_id],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        env=environment,
        **options,
    )


def _message(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stderr or result.stdout or f"exit code {result.returncode}").strip()


def _at_host(host: str, args: list[str]) -> subprocess.CompletedProcess[str]:
    return _docker_run(["--host", host, *args])


def _inspect_object(container_id: str, host: str) -> dict[str, Any]:
    result = _at_host(host, ["inspect", container_id])
    if result.returncode != 0:
        raise DockerError(f"Cannot inspect Docker container {container_id}: {_message(result)}")
    try:
        value = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise DockerError(f"Docker returned invalid inspection data for {container_id}") from exc
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise DockerError(f"Docker returned unexpected inspection data for {container_id}")
    return value[0]


def _known_missing(result: subprocess.CompletedProcess[str]) -> bool:
    message = f"{result.stdout}\n{result.stderr}".lower()
    return "no such object" in message or "no such container" in message


def _inspect_if_present(identifier: str, host: str) -> dict[str, Any] | None:
    result = _at_host(host, ["inspect", identifier])
    if result.returncode != 0:
        if _known_missing(result):
            return None
        raise DockerError(f"Cannot inspect Docker container {identifier}: {_message(result)}")
    try:
        value = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise DockerError(f"Docker returned invalid inspection data for {identifier}") from exc
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise DockerError(f"Docker returned unexpected inspection data for {identifier}")
    return value[0]


def _container_labels(value: dict[str, Any]) -> dict[str, str]:
    config = value.get("Config")
    labels = config.get("Labels") if isinstance(config, dict) else None
    if not isinstance(labels, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in labels.items()
    ):
        return {}
    return labels


def _run_slug(run_id: str) -> str:
    if not _RUN_ID.fullmatch(run_id):
        raise DockerError("run_id must contain only letters, digits, dot, underscore, or hyphen")
    slug = re.sub(r"[^a-z0-9]", "-", run_id.lower()).strip("-")[:12]
    if not slug:
        raise DockerError("run_id does not contain a usable container-name component")
    return slug


def _expected_name(namespace: str, run_id: str, suffix: str) -> str:
    return f"katydid-{namespace}-{_run_slug(run_id)}-{suffix}"


def _validate_owned(
    value: dict[str, Any],
    container_id: str,
    namespace: str,
    run_id: str,
    name: str,
    expires_at: int | None = None,
) -> None:
    if value.get("Id") != container_id:
        raise DockerError("Docker container identity changed before cleanup")
    actual_name = value.get("Name")
    if not isinstance(actual_name, str) or actual_name.lstrip("/") != name:
        raise DockerError("Docker container name changed before cleanup")
    labels = _container_labels(value)
    required = {
        MANAGED_LABEL: MANAGED_VALUE,
        NAMESPACE_LABEL: namespace,
        RUN_LABEL: run_id,
    }
    if any(labels.get(key) != expected for key, expected in required.items()):
        raise DockerError("Docker container ownership labels changed before cleanup")
    expiry = labels.get(EXPIRY_LABEL)
    if expiry is None or not expiry.isascii() or not expiry.isdigit():
        raise DockerError("Docker container expiry label is malformed")
    if expires_at is not None and expiry != str(expires_at):
        raise DockerError("Docker container expiry label changed before cleanup")


def _mount(path: Path, target: str, *, readonly: bool = False) -> str:
    source = str(path.resolve())
    if any(character in source for character in (",", "\n", "\r", "\x00")):
        raise DockerError("Docker bind-mount source contains an unsupported character")
    option = f"type=bind,source={source},target={target}"
    return f"{option},readonly" if readonly else option


def _is_link_or_reparse(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _local_endpoint(value: str) -> bool:
    if value.startswith("unix://"):
        return True
    lowered = value.lower()
    return lowered.startswith("npipe:////./pipe/") or lowered.startswith("npipe://./pipe/")


def _active_local_host() -> str:
    overridden = os.environ.get("DOCKER_HOST")
    if overridden is not None:
        if not _local_endpoint(overridden):
            raise DockerError("Docker isolation requires a local Unix-socket or named-pipe daemon")
        return overridden
    context = _docker_run(["context", "inspect", "--format", "{{json .Endpoints.docker.Host}}"])
    if context.returncode != 0:
        raise DockerError(f"Cannot inspect the active Docker context: {_message(context)}")
    try:
        context_host = json.loads(context.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise DockerError("Docker returned invalid active-context data") from exc
    if not isinstance(context_host, str) or not _local_endpoint(context_host):
        raise DockerError("Docker isolation requires a local Unix-socket or named-pipe daemon")
    return context_host


def _canonical_repository(value: str) -> str:
    repository = value.rsplit("@sha256:", 1)[0]
    last = repository.rsplit("/", 1)[-1]
    if ":" in last:
        repository = repository.rsplit(":", 1)[0]
    for prefix in ("docker.io/", "index.docker.io/", "registry-1.docker.io/"):
        if repository.startswith(prefix):
            repository = repository[len(prefix) :]
            break
    if repository.startswith("library/"):
        repository = repository[len("library/") :]
    return repository


def _capture_stream(
    stream: BinaryIO,
    destination: Path,
    count: list[int],
    lock: threading.Lock,
    overflow: threading.Event,
    failures: list[str],
) -> None:
    try:
        with destination.open("wb") as output:
            while True:
                chunk = stream.read(64 * 1024)
                if not chunk:
                    return
                with lock:
                    remaining = MAX_LOG_BYTES - count[0]
                    if remaining > 0:
                        written = chunk[:remaining]
                        output.write(written)
                        count[0] += len(written)
                    if len(chunk) > max(remaining, 0):
                        overflow.set()
    except (OSError, ValueError) as exc:
        failures.append(str(exc))
        overflow.set()


def _safe_report(source: Path, destination: Path) -> TestEvidence:
    """Copy untrusted container evidence through a bounded, no-link regular-file read."""
    try:
        parent = source.parent
        parent_info = parent.lstat()
        if not stat.S_ISDIR(parent_info.st_mode) or _is_link_or_reparse(parent_info):
            raise ValueError("JUnit output directory must be a real directory")
        before = source.lstat()
        if not stat.S_ISREG(before.st_mode) or _is_link_or_reparse(before):
            raise ValueError("JUnit evidence must be a regular file, not a link or device")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(source, flags)
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or _is_link_or_reparse(opened)
                or getattr(before, "st_ino", None) != getattr(opened, "st_ino", None)
                or getattr(before, "st_dev", None) != getattr(opened, "st_dev", None)
            ):
                raise ValueError("JUnit evidence changed before collection")
            raw = bytearray()
            while len(raw) <= MAX_REPORT_BYTES:
                chunk = os.read(descriptor, min(1024 * 1024, MAX_REPORT_BYTES + 1 - len(raw)))
                if not chunk:
                    break
                raw.extend(chunk)
            if len(raw) > MAX_REPORT_BYTES:
                raise ValueError("JUnit report exceeds the 10 MiB limit")
        finally:
            os.close(descriptor)
        destination.write_bytes(raw)
        return read_junit(destination)
    except FileNotFoundError:
        return TestEvidence(Status.MISSING_EVIDENCE, "Expected JUnit report was not produced")
    except (OSError, ValueError) as exc:
        return TestEvidence(
            Status.INVALID_EVIDENCE, f"Cannot safely collect Docker JUnit evidence: {exc}"
        )


class DockerSession:
    """One run's source snapshot and fresh-container-per-check execution state."""

    def __init__(
        self,
        plan: Plan,
        directory: Path,
        run_id: str,
        write_json: Callable[[Path, Any], None],
    ) -> None:
        if plan.isolation is None:
            raise DockerError("DockerSession requires an isolation plan")
        self.plan = plan
        self.specification: PlannedIsolation = plan.isolation
        self.directory = directory.resolve()
        self.run_id = run_id
        self.write_json = write_json
        self.isolation_directory = self.directory / "isolation"
        self.source_directory = self.isolation_directory / "source"
        self.ready = False
        self.errors: list[str] = []
        self.resources: list[dict[str, Any]] = []
        self._image_id: str | None = None
        self._daemon: dict[str, str] | None = None
        self._docker_host: str | None = None
        if self.specification.adapter != "docker":
            raise DockerError(f"Unsupported isolation adapter: {self.specification.adapter}")
        if not _NAMESPACE.fullmatch(self.specification.namespace):
            raise DockerError("Docker namespace is invalid")
        _run_slug(run_id)

    @property
    def cleanup_complete(self) -> bool:
        return all(bool(resource.get("removed")) for resource in self.resources)

    def snapshot(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "cleanup_complete": self.cleanup_complete,
            "errors": list(self.errors),
            "resources": [dict(resource) for resource in self.resources],
            "image_id": self._image_id,
            "daemon": dict(self._daemon) if self._daemon is not None else None,
        }

    def _changed(self) -> None:
        self.isolation_directory.mkdir(parents=True, exist_ok=True)
        self.write_json(self.isolation_directory / "isolation.json", self.snapshot())

    def _fail(self, message: str) -> DockerError:
        self.errors.append(message)
        self._changed()
        return DockerError(message)

    def _run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        if self._docker_host is None:
            raise DockerError("Docker endpoint was not pinned during preflight")
        return _at_host(self._docker_host, args)

    def _preflight(self) -> None:
        effective_host = _active_local_host()
        self._docker_host = effective_host

        version = self._run(["version", "--format", "{{json .Server}}"])
        if version.returncode != 0:
            raise DockerError(f"Docker Linux daemon is unavailable: {_message(version)}")
        try:
            server = json.loads(version.stdout)
        except (TypeError, json.JSONDecodeError) as exc:
            raise DockerError("Docker returned invalid daemon version data") from exc
        if not isinstance(server, dict) or str(server.get("Os", "")).lower() != "linux":
            raise DockerError("Docker isolation requires a Linux container daemon")
        daemon_version = server.get("Version")
        if not isinstance(daemon_version, str) or not daemon_version:
            raise DockerError("Docker daemon version is unavailable")
        self._daemon = {"os": "linux", "version": daemon_version, "host": effective_host}

        inspected = self._run(["image", "inspect", self.specification.image])
        if inspected.returncode != 0:
            raise DockerError(
                "Digest-pinned Docker image is not installed locally; Katydid never pulls images: "
                f"{_message(inspected)}"
            )
        try:
            values = json.loads(inspected.stdout)
        except (TypeError, json.JSONDecodeError) as exc:
            raise DockerError("Docker returned invalid image inspection data") from exc
        if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], dict):
            raise DockerError("Docker returned unexpected image inspection data")
        image = values[0]
        image_id = image.get("Id")
        digests = image.get("RepoDigests")
        config = image.get("Config")
        if not isinstance(image_id, str) or not _IMAGE_ID.fullmatch(image_id):
            raise DockerError("Docker image has no immutable image ID")
        requested_digest = self.specification.image.rsplit("@sha256:", 1)[-1]
        requested_repository = _canonical_repository(self.specification.image)
        matching_digest = any(
            isinstance(digest, str)
            and digest.endswith(f"@sha256:{requested_digest}")
            and _canonical_repository(digest) == requested_repository
            for digest in digests or ()
        )
        if not isinstance(digests, list) or not matching_digest:
            raise DockerError(
                "Installed Docker image does not match the requested repository digest"
            )
        if str(image.get("Os", "")).lower() != "linux":
            raise DockerError("Docker image is not a Linux image")
        if not isinstance(config, dict):
            raise DockerError("Docker image configuration is unavailable")
        volumes = config.get("Volumes")
        if volumes not in (None, {}):
            raise DockerError("Docker images declaring automatic volumes are not supported")
        self._image_id = image_id

    def _copy_source(self) -> None:
        root = Path(self.plan.root).resolve(strict=True)
        if self.source_directory.exists():
            raise DockerError("Isolation source snapshot already exists")
        self.source_directory.mkdir(parents=True)
        total = 0
        modes: dict[Path, int] = {}
        for filename in self.specification.files:
            try:
                source = isolated_source(root, filename)
                before = source.lstat()
                if not stat.S_ISREG(before.st_mode):
                    raise ProfileError(f"Isolation source is not a regular file: {filename}")
                target = self.source_directory / filename
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.open("rb") as input_stream, target.open("xb") as output_stream:
                    opened = os.fstat(input_stream.fileno())
                    if not stat.S_ISREG(opened.st_mode) or (
                        getattr(before, "st_ino", None) != getattr(opened, "st_ino", None)
                        or getattr(before, "st_dev", None) != getattr(opened, "st_dev", None)
                    ):
                        raise DockerError(f"Isolation source changed while copying: {filename}")
                    copied = 0
                    while True:
                        remaining = SOURCE_LIMIT_BYTES - total - copied
                        if remaining < 0:
                            raise DockerError("Isolation source snapshot exceeds the 64 MiB limit")
                        chunk = input_stream.read(min(1024 * 1024, remaining + 1))
                        if not chunk:
                            break
                        copied += len(chunk)
                        if total + copied > SOURCE_LIMIT_BYTES:
                            raise DockerError("Isolation source snapshot exceeds the 64 MiB limit")
                        output_stream.write(chunk)
                    after = os.fstat(input_stream.fileno())
                    if (
                        copied != before.st_size
                        or after.st_size != before.st_size
                        or after.st_mtime_ns != before.st_mtime_ns
                    ):
                        raise DockerError(f"Isolation source changed while copying: {filename}")
                    total += copied
                    modes[target] = before.st_mode
            except (OSError, ProfileError) as exc:
                raise DockerError(f"Cannot snapshot isolation source {filename}: {exc}") from exc
        # The snapshot never needs to be writable by either the host command or container user.
        for item in sorted(self.source_directory.rglob("*"), reverse=True):
            try:
                mode = 0o555 if item.is_dir() or modes.get(item, 0) & 0o111 else 0o444
                item.chmod(mode)
            except OSError as exc:
                raise DockerError(f"Cannot make isolation snapshot read-only: {exc}") from exc
        self.source_directory.chmod(0o555)

    def prepare(self) -> None:
        try:
            self.isolation_directory.mkdir(parents=True, exist_ok=True)
            self._preflight()
            self._copy_source()
            self.ready = True
            self._changed()
        except DockerError as exc:
            self.ready = False
            raise self._fail(str(exc)) from exc
        except (OSError, subprocess.SubprocessError) as exc:
            self.ready = False
            raise self._fail(f"Cannot prepare Docker isolation: {exc}") from exc

    def _container_command(
        self,
        check: PlannedCheck,
        output_directory: Path,
        environment_directory: Path | None,
        name: str,
        expires_at: int,
    ) -> tuple[list[str], list[str], str]:
        root = Path(self.plan.root).resolve()
        working = Path(check.working_directory).resolve()
        try:
            relative = working.relative_to(root)
        except ValueError as exc:
            raise DockerError("Working directory changed or escaped the source root") from exc
        container_working = (
            "/workspace" if relative == Path(".") else f"/workspace/{relative.as_posix()}"
        )
        snapshot_working = self.source_directory / relative
        if not snapshot_working.is_dir():
            raise DockerError("Working directory is absent from the isolation snapshot")

        argv: list[str] = []
        for value in check.argv:
            if value == "{python}":
                value = "python3"
            else:
                value = value.replace("{report}", "/output/junit.xml").replace(
                    "{run_id}", self.run_id
                )
                if "{environment}" in value:
                    if environment_directory is None:
                        raise DockerError("{environment} requires environment data")
                    value = value.replace("{environment}", "/environment")
            argv.append(value)
        if not argv or not argv[0]:
            raise DockerError("Docker check has no executable")

        labels = {
            MANAGED_LABEL: MANAGED_VALUE,
            NAMESPACE_LABEL: self.specification.namespace,
            RUN_LABEL: self.run_id,
            EXPIRY_LABEL: str(expires_at),
        }
        command = ["create", "--name", name]
        for key, value in labels.items():
            command.extend(["--label", f"{key}={value}"])
        command.extend(
            [
                "--network",
                "none",
                "--user",
                "65532:65532",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--cpus",
                str(self.specification.cpus),
                "--memory",
                f"{self.specification.memory_mb}m",
                "--memory-swap",
                f"{self.specification.memory_mb}m",
                "--pids-limit",
                str(self.specification.pids_limit),
                "--tmpfs",
                f"/tmp:rw,nosuid,nodev,noexec,size={self.specification.tmpfs_mb}m",
                "--no-healthcheck",
                "--log-driver",
                "none",
                "--workdir",
                container_working,
                "--mount",
                _mount(self.source_directory, "/workspace", readonly=True),
                "--mount",
                _mount(output_directory, "/output"),
                "--env",
                f"KATYDID_RUN_ID={self.run_id}",
                "--env",
                "KATYDID_REPORT_PATH=/output/junit.xml",
                "--env",
                "PYTHONUNBUFFERED=1",
                "--env",
                "PYTHONDONTWRITEBYTECODE=1",
                "--env",
                "PYTHONPYCACHEPREFIX=/tmp/pycache",
                "--env",
                "HOME=/tmp",
            ]
        )
        if environment_directory is not None:
            command.extend(
                [
                    "--mount",
                    _mount(environment_directory, "/environment"),
                    "--env",
                    "KATYDID_ENVIRONMENT_DIR=/environment",
                ]
            )
        # Refer to the immutable local image ID after verifying the requested RepoDigest.
        command.extend(["--entrypoint", argv[0], self._image_id or self.specification.image])
        command.extend(argv[1:])
        return command, argv, container_working

    def _resource(self, name: str, expires_at: int) -> dict[str, Any]:
        labels = {
            MANAGED_LABEL: MANAGED_VALUE,
            NAMESPACE_LABEL: self.specification.namespace,
            RUN_LABEL: self.run_id,
            EXPIRY_LABEL: str(expires_at),
        }
        resource: dict[str, Any] = {
            "id": None,
            "name": name,
            "expires_at": expires_at,
            "labels": labels,
            "removed": False,
        }
        return resource

    def _remove(self, resource: dict[str, Any]) -> None:
        name = str(resource["name"])
        raw_id = resource.get("id")
        identifier = str(raw_id) if isinstance(raw_id, str) else name
        if self._docker_host is None:
            raise DockerError("Docker endpoint was not pinned during preflight")
        inspected = _inspect_if_present(identifier, self._docker_host)
        if inspected is None:
            resource["removed"] = True
            self._changed()
            return
        container_id = inspected.get("Id")
        if not isinstance(container_id, str) or not _CONTAINER_ID.fullmatch(container_id):
            raise DockerError("Docker container has no immutable ID")
        _validate_owned(
            inspected,
            container_id,
            self.specification.namespace,
            self.run_id,
            name,
            int(resource["expires_at"]),
        )
        resource["id"] = container_id
        removed = self._run(["rm", "--force", container_id])
        if removed.returncode != 0:
            if _known_missing(removed):
                resource["removed"] = True
                self._changed()
                return
            raise DockerError(f"Cannot remove Docker container {container_id}: {_message(removed)}")
        verification = self._run(["inspect", container_id])
        if verification.returncode == 0:
            raise DockerError(f"Docker container {container_id} still exists after removal")
        if not _known_missing(verification):
            raise DockerError(
                "Cannot verify Docker container removal for "
                f"{container_id}: {_message(verification)}"
            )
        resource["removed"] = True
        self._changed()

    def execute(
        self,
        check: PlannedCheck,
        folder: Path,
        cancel: Any,
        cancel_file: Path,
        environment_directory: Path | None = None,
    ) -> CheckResult:
        started = time.monotonic()
        if not self.ready or self._image_id is None:
            return CheckResult(check.id, Status.ERROR, "Docker isolation was not prepared")
        folder.mkdir(parents=True, exist_ok=True)
        output_directory = folder / "output"
        output_directory.mkdir(exist_ok=False)
        try:
            output_directory.chmod(0o777)
            if environment_directory is not None:
                environment_directory.mkdir(parents=True, exist_ok=True)
                environment_directory.chmod(0o777)
        except OSError as exc:
            message = f"Cannot prepare Docker writable directories: {exc}"
            self.errors.append(message)
            self._changed()
            return CheckResult(check.id, Status.ERROR, message)

        stdout_path = folder / "stdout.log"
        stderr_path = folder / "stderr.log"
        expires_at = int(_now()) + check.timeout_seconds + 60
        suffix = secrets.token_hex(6)
        name = _expected_name(self.specification.namespace, self.run_id, suffix)
        process: subprocess.Popen[bytes] | None = None
        capture_threads: list[threading.Thread] = []
        capture_failures: list[str] = []
        log_overflow = threading.Event()
        resource: dict[str, Any] | None = None
        status: Status | None = None
        detail = ""
        exit_code: int | None = None
        deferred: list[BaseException] = []

        def remember(message: str) -> None:
            self.errors.append(message)
            try:
                self._changed()
            except BaseException as exc:
                deferred.append(exc)

        try:
            command, argv, working = self._container_command(
                check, output_directory, environment_directory, name, expires_at
            )
            self.write_json(
                folder / "invocation.json",
                {
                    "argv": argv,
                    "cwd": working,
                    "timeout_seconds": check.timeout_seconds,
                    "image": self.specification.image,
                },
            )
            if cancel.is_set() or cancel_file.exists():
                return CheckResult(check.id, Status.CANCELLED, "Cancelled before container launch")
            # Journal the unique name and fixed expiry before asking Docker to create anything.
            # If the CLI loses its response, cleanup can resolve this exact name to an immutable
            # ID and then revalidate every ownership label before removal.
            resource = self._resource(name, expires_at)
            self.resources.append(resource)
            self._changed()
            created = self._run(command)
            if created.returncode != 0:
                raise DockerError(f"Cannot create Docker container: {_message(created)}")
            container_id = created.stdout.strip()
            if not _CONTAINER_ID.fullmatch(container_id):
                raise DockerError("Docker create did not return an immutable container ID")
            resource["id"] = container_id
            # Persist the immutable ID and fixed expiry before starting remote work.
            self._changed()
            assert self._docker_host is not None
            inspected = _inspect_object(container_id, self._docker_host)
            _validate_owned(
                inspected,
                container_id,
                self.specification.namespace,
                self.run_id,
                name,
                expires_at,
            )
            self.write_json(folder / "container.json", dict(resource))
            process = _docker_start(container_id, self._docker_host)
            if process.stdout is None or process.stderr is None:
                raise DockerError("Docker attach did not provide bounded output streams")
            count = [0]
            lock = threading.Lock()
            for stream, destination in (
                (process.stdout, stdout_path),
                (process.stderr, stderr_path),
            ):
                thread = threading.Thread(
                    target=_capture_stream,
                    args=(stream, destination, count, lock, log_overflow, capture_failures),
                    name=f"katydid-docker-{check.id}-{len(capture_threads)}",
                    daemon=True,
                )
                capture_threads.append(thread)
                thread.start()
            while True:
                if cancel.is_set() or cancel_file.exists():
                    status, detail = Status.CANCELLED, "Cancellation requested"
                    break
                if log_overflow.is_set():
                    if capture_failures:
                        detail = f"Cannot capture Docker logs: {capture_failures[0]}"
                    else:
                        detail = "Combined process logs exceeded 10 MiB"
                    status = Status.ERROR
                    remember(detail)
                    break
                if process.poll() is not None:
                    break
                if time.monotonic() - started >= check.timeout_seconds:
                    status, detail = (
                        Status.TIMED_OUT,
                        "Container exceeded its configured timeout",
                    )
                    break
                time.sleep(POLL_SECONDS)
            if status is None:
                process.wait(timeout=5)
                for thread in capture_threads:
                    thread.join(timeout=5)
                if any(thread.is_alive() for thread in capture_threads):
                    raise DockerError("Docker output capture did not terminate")
                if log_overflow.is_set():
                    if capture_failures:
                        raise DockerError(f"Cannot capture Docker logs: {capture_failures[0]}")
                    raise DockerError("Combined process logs exceeded 10 MiB")
                inspected = _inspect_object(container_id, self._docker_host)
                state = inspected.get("State")
                if not isinstance(state, dict) or state.get("Running") is not False:
                    raise DockerError("Docker container did not reach a stopped state")
                value = state.get("ExitCode")
                if type(value) is not int:
                    raise DockerError("Docker container has no trustworthy exit code")
                exit_code = value
                state_error = state.get("Error")
                if state.get("OOMKilled") is True:
                    raise DockerError("Docker container was killed after exceeding memory")
                if isinstance(state_error, str) and state_error:
                    raise DockerError(f"Docker runtime reported an error: {state_error}")
        except KeyboardInterrupt:
            cancel.set()
            status, detail = Status.CANCELLED, "Interrupted"
        except (OSError, subprocess.SubprocessError, DockerError) as exc:
            status, detail = Status.ERROR, f"Docker execution failed: {exc}"
            remember(detail)
        finally:
            if resource is not None and not resource.get("removed"):
                try:
                    # Removing the remote container is what stops its process.  Killing the
                    # local `docker start --attach` client is not a containment operation.
                    self._remove(resource)
                except BaseException as exc:
                    cleanup = f"Docker container cleanup failed: {exc}"
                    remember(cleanup)
                    if not isinstance(exc, (OSError, subprocess.SubprocessError, DockerError)):
                        deferred.append(exc)
                    status = Status.ERROR
                    detail = f"{detail}; {cleanup}" if detail else cleanup
            if process is not None and process.poll() is None:
                try:
                    process.kill()
                    process.wait(timeout=5)
                except BaseException as exc:
                    cleanup = f"Docker client cleanup failed: {exc}"
                    remember(cleanup)
                    if not isinstance(exc, (OSError, subprocess.SubprocessError)):
                        deferred.append(exc)
                    status = Status.ERROR
                    detail = f"{detail}; {cleanup}" if detail else cleanup
            for thread in capture_threads:
                thread.join(timeout=5)
            if any(thread.is_alive() for thread in capture_threads):
                cleanup = "Docker output capture did not terminate"
                remember(cleanup)
                status = Status.ERROR
                detail = f"{detail}; {cleanup}" if detail else cleanup

        if resource is not None:
            try:
                self.write_json(folder / "container.json", dict(resource))
            except BaseException as exc:
                persistence = f"Cannot persist final Docker container state: {exc}"
                remember(persistence)
                if not isinstance(exc, OSError):
                    deferred.append(exc)
                status = Status.ERROR
                detail = f"{detail}; {persistence}" if detail else persistence

        if deferred:
            raise DockerError(f"Cannot persist Docker cleanup state: {deferred[0]}") from deferred[
                0
            ]

        elapsed = round(time.monotonic() - started, 6)
        report = output_directory / "junit.xml"
        evidence = _safe_report(report, folder / "junit.xml") if check.kind == "test" else None
        if evidence is not None and evidence.detail.startswith(
            "Cannot safely collect Docker JUnit evidence:"
        ):
            collection = f"Docker evidence collection failed: {evidence.detail}"
            self.errors.append(collection)
            self._changed()
        if status is None:
            if exit_code != 0:
                status, detail = Status.FAILED, f"Container exited with code {exit_code}"
            elif evidence is not None:
                status, detail = evidence.status, evidence.detail
            else:
                status, detail = Status.PASSED, "Command exited successfully in Docker isolation"
        return CheckResult(check.id, status, detail, exit_code, elapsed, evidence)

    def cleanup(self) -> None:
        """Best-effort cleanup for callers interrupted between create and execute's finally."""
        deferred: list[BaseException] = []
        for resource in self.resources:
            if resource.get("removed"):
                continue
            try:
                self._remove(resource)
            except BaseException as exc:
                self.errors.append(f"Docker container cleanup failed: {exc}")
                try:
                    self._changed()
                except BaseException as persistence:
                    deferred.append(persistence)
                if not isinstance(exc, (OSError, subprocess.SubprocessError, DockerError)):
                    deferred.append(exc)
        if deferred:
            raise DockerError(f"Cannot persist Docker cleanup state: {deferred[0]}") from deferred[
                0
            ]


def _sweep_name(namespace: str, run_id: str, name: str) -> bool:
    prefix = f"katydid-{namespace}-{_run_slug(run_id)}-"
    suffix = name.lstrip("/")[len(prefix) :] if name.lstrip("/").startswith(prefix) else ""
    return bool(re.fullmatch(r"[a-f0-9]{12}", suffix))


def sweep(namespace: str) -> dict[str, list[str]]:
    """Remove expired, correctly owned containers from exactly one Katydid namespace."""
    result: dict[str, list[str]] = {"removed": [], "skipped": [], "errors": []}
    if not _NAMESPACE.fullmatch(namespace):
        result["errors"].append("Invalid Docker sweep namespace")
        return result
    try:
        host = _active_local_host()
        listed = _at_host(
            host,
            [
                "ps",
                "--all",
                "--no-trunc",
                "--filter",
                f"label={MANAGED_LABEL}={MANAGED_VALUE}",
                "--filter",
                f"label={NAMESPACE_LABEL}={namespace}",
                "--format",
                "{{.ID}}",
            ],
        )
    except (OSError, subprocess.SubprocessError, DockerError) as exc:
        result["errors"].append(f"Cannot list Docker containers: {exc}")
        return result
    if listed.returncode != 0:
        result["errors"].append(f"Cannot list Docker containers: {_message(listed)}")
        return result
    for candidate in listed.stdout.splitlines():
        container_id = candidate.strip()
        if not _CONTAINER_ID.fullmatch(container_id):
            if container_id:
                result["skipped"].append(container_id)
            continue
        try:
            inspected = _inspect_if_present(container_id, host)
            if inspected is None:
                result["skipped"].append(container_id)
                continue
            labels = _container_labels(inspected)
            run_id = labels.get(RUN_LABEL, "")
            expiry = labels.get(EXPIRY_LABEL, "")
            name = inspected.get("Name")
            valid = (
                inspected.get("Id") == container_id
                and labels.get(MANAGED_LABEL) == MANAGED_VALUE
                and labels.get(NAMESPACE_LABEL) == namespace
                and bool(_RUN_ID.fullmatch(run_id))
                and expiry.isascii()
                and expiry.isdigit()
                and len(expiry) <= 20
                and expiry == str(int(expiry))
                and isinstance(name, str)
                and _sweep_name(namespace, run_id, name)
            )
            if not valid or int(expiry) > int(_now()):
                result["skipped"].append(container_id)
                continue
            assert isinstance(name, str)
            _validate_owned(inspected, container_id, namespace, run_id, name.lstrip("/"))
            removed = _at_host(host, ["rm", "--force", container_id])
            if removed.returncode != 0:
                if _known_missing(removed):
                    result["removed"].append(container_id)
                    continue
                raise DockerError(f"Cannot remove Docker container: {_message(removed)}")
            verification = _at_host(host, ["inspect", container_id])
            if verification.returncode == 0:
                raise DockerError("Docker container still exists after sweeping")
            missing = f"{verification.stdout}\n{verification.stderr}".lower()
            if "no such object" not in missing and "no such container" not in missing:
                raise DockerError(f"Cannot verify removal: {_message(verification)}")
            result["removed"].append(container_id)
        except (OSError, subprocess.SubprocessError, DockerError, ValueError) as exc:
            result["errors"].append(f"{container_id}: {exc}")
    return result
