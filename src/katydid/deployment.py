"""Managed single-host releases with exact artifacts and owned processes."""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, NoReturn, cast
from urllib.parse import urlsplit

SCHEMA_VERSION = 1
REGISTRY_NAME = "managed-release.json"
MAX_SPEC_BYTES = 64 * 1024
MAX_HEALTH_BYTES = 64 * 1024
GIT_TIMEOUT_SECONDS = 60
START_TIMEOUT_SECONDS = 15.0
LOG_FILE_BYTES = 5 * 1024 * 1024
LOG_FILE_COUNT = 3
LOG_READ_BYTES = 64 * 1024
LOG_DRAIN_TIMEOUT_SECONDS = 2.0
_SHA = re.compile(r"[0-9a-f]{40,64}\Z")
_ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_PLACEHOLDER = re.compile(r"\{[^{}]+\}")
_ALLOWED_PLACEHOLDERS = frozenset({"{commit}", "{data}", "{python}", "{release}"})
_RESERVED_ENVIRONMENT = frozenset(
    {
        "KATYDID_DATA_DIR",
        "KATYDID_DEPLOYMENT_TOKEN",
        "KATYDID_RELEASE_COMMIT",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONPYCACHEPREFIX",
    }
)
_INHERITED_ENVIRONMENT = frozenset(
    {
        "APPDATA",
        "COMSPEC",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "LOCALAPPDATA",
        "PATH",
        "PATHEXT",
        "PROGRAMDATA",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TZ",
        "USERPROFILE",
        "WINDIR",
    }
)


class DeploymentError(RuntimeError):
    """A managed release could not be proved safe or healthy."""


def _fail(message: str) -> NoReturn:
    raise DeploymentError(message)


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}-",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class _RotatingLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.stream: BinaryIO | None = None
        self.size = 0

    def __enter__(self) -> _RotatingLog:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for generation in range(LOG_FILE_COUNT):
            existing = (
                self.path
                if generation == 0
                else self.path.with_name(f"{self.path.name}.{generation}")
            )
            if existing.exists() and existing.stat().st_size > LOG_FILE_BYTES:
                with existing.open("r+b", buffering=0) as stream:
                    stream.seek(-LOG_FILE_BYTES, os.SEEK_END)
                    tail = stream.read(LOG_FILE_BYTES)
                    stream.seek(0)
                    stream.write(tail)
                    stream.truncate()
        self.size = self.path.stat().st_size if self.path.exists() else 0
        if self.size >= LOG_FILE_BYTES:
            self._rotate()
        else:
            self.stream = self.path.open("ab", buffering=0)
        return self

    def __exit__(self, *_error: object) -> None:
        if self.stream is not None:
            self.stream.close()
            self.stream = None

    def _rotate(self) -> None:
        if self.stream is not None:
            self.stream.close()
            self.stream = None
        oldest = self.path.with_name(f"{self.path.name}.{LOG_FILE_COUNT - 1}")
        oldest.unlink(missing_ok=True)
        for generation in range(LOG_FILE_COUNT - 2, 0, -1):
            source = self.path.with_name(f"{self.path.name}.{generation}")
            if source.exists():
                source.replace(self.path.with_name(f"{self.path.name}.{generation + 1}"))
        if self.path.exists():
            self.path.replace(self.path.with_name(f"{self.path.name}.1"))
        self.stream = self.path.open("wb", buffering=0)
        self.size = 0

    def write(self, value: bytes) -> None:
        pending = memoryview(value)
        while pending:
            if self.size >= LOG_FILE_BYTES:
                self._rotate()
            length = min(len(pending), LOG_FILE_BYTES - self.size)
            if self.stream is None:
                _fail("Rotating log is not open")
            written = self.stream.write(pending[:length])
            if written is None or written <= 0:
                _fail("Rotating log write made no progress")
            self.size += written
            pending = pending[written:]


def _drain_rotating(pipe: BinaryIO, path: Path) -> None:
    try:
        with _RotatingLog(path) as output:
            while chunk := pipe.read(LOG_READ_BYTES):
                output.write(chunk)
    except (OSError, DeploymentError):
        # Continue draining after a logging failure so the application cannot block
        # forever on a full stdout or stderr pipe.
        try:
            while pipe.read(LOG_READ_BYTES):
                pass
        except (OSError, ValueError):
            pass
    finally:
        pipe.close()


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail(f"Duplicate JSON field: {key}")
        result[key] = value
    return result


def _read_json(path: Path, *, maximum: int = MAX_SPEC_BYTES) -> dict[str, Any]:
    try:
        if path.stat().st_size > maximum:
            _fail(f"JSON file exceeds {maximum} bytes: {path}")
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_pairs)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DeploymentError(f"Cannot read JSON file {path}: {exc}") from exc
    if not isinstance(value, dict):
        _fail(f"JSON document must be an object: {path}")
    return value


@contextlib.contextmanager
def _state_lock(state: Path) -> Iterator[None]:
    state.mkdir(parents=True, exist_ok=True)
    path = state / ".managed-release.lock"
    with path.open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _run(argv: Sequence[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            list(argv),
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DeploymentError(f"Cannot run {argv[0]}: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic"
        _fail(f"{argv[0]} exited with code {result.returncode}: {detail}")
    return result


def _git(workspace: Path, *args: str) -> str:
    return _run(["git", *args], workspace).stdout.strip()


def _validate_commit(value: str) -> str:
    if not _SHA.fullmatch(value):
        _fail("Commit must be a lowercase 40-64 character hexadecimal object ID")
    return value


def _validate_workspace(workspace: Path, commit: str) -> Path:
    try:
        workspace = workspace.resolve(strict=True)
        if workspace.is_symlink() or not workspace.is_dir():
            _fail("Workspace must be a real directory")
    except (OSError, RuntimeError) as exc:
        raise DeploymentError(f"Cannot resolve workspace: {exc}") from exc
    if _git(workspace, "rev-parse", "HEAD") != commit:
        _fail("Workspace HEAD does not equal the requested release commit")
    if _git(workspace, "cat-file", "-t", commit) != "commit":
        _fail("Requested release object is not a Git commit")
    dirty = _git(workspace, "status", "--porcelain", "--untracked-files=all", "--ignored")
    if dirty:
        _fail("Workspace must be clean, including untracked and ignored files")
    return workspace


def _inside(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def _validate_paths(workspace: Path, state: Path, spec: Path) -> tuple[Path, Path]:
    try:
        state = state.resolve()
        spec = spec.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise DeploymentError(f"Cannot resolve managed release paths: {exc}") from exc
    if _inside(state, workspace):
        _fail("Managed state must be outside the Git workspace")
    if _inside(spec, workspace):
        _fail("Deployment spec must be host-owned outside the Git workspace")
    return state, spec


def _validate_template(value: str) -> str:
    unknown = set(_PLACEHOLDER.findall(value)) - _ALLOWED_PLACEHOLDERS
    if unknown:
        _fail(f"Unknown deployment placeholder: {sorted(unknown)[0]}")
    return value


def _relative_directory(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        _fail("working_directory must be a relative POSIX directory")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", "..") for part in path.parts):
        _fail("working_directory must stay inside the release")
    return path.as_posix()


def _bounded_number(value: Any, name: str, minimum: float, maximum: float, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(f"{name} must be a number")
    result = float(value)
    if not minimum <= result <= maximum:
        _fail(f"{name} must be between {minimum} and {maximum}")
    return result


def _validate_health(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail("health must be an object")
    allowed = {
        "url",
        "expected_body",
        "expected_headers",
        "timeout_seconds",
        "interval_seconds",
        "request_timeout_seconds",
    }
    unknown = set(value) - allowed
    if unknown:
        _fail(f"Unknown health field: {sorted(unknown)[0]}")
    url = value.get("url")
    if not isinstance(url, str) or len(url) > 2048:
        _fail("health.url must be a bounded string")
    _validate_template(url)
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise DeploymentError(f"health.url has an invalid port: {exc}") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        _fail("health.url must be an explicit loopback HTTP endpoint")
    expected_body = value.get("expected_body")
    if expected_body is not None and not isinstance(expected_body, str):
        _fail("health.expected_body must be a string")
    if isinstance(expected_body, str):
        _validate_template(expected_body)
    raw_headers = value.get("expected_headers", {})
    if not isinstance(raw_headers, dict) or len(raw_headers) > 20:
        _fail("health.expected_headers must be a bounded object")
    headers: dict[str, str] = {}
    for name, expected in raw_headers.items():
        if (
            not isinstance(name, str)
            or not name
            or any(character in name for character in "\r\n:")
            or not isinstance(expected, str)
        ):
            _fail("Health header names and values must be strings")
        headers[name] = _validate_template(expected)
    proof_values = [expected_body or "", *headers.values()]
    if not any("{commit}" in item for item in proof_values):
        _fail("Health verification must bind a response expectation to {commit}")
    return {
        "url": url,
        "expected_body": expected_body,
        "expected_headers": headers,
        "timeout_seconds": _bounded_number(
            value.get("timeout_seconds"), "health.timeout_seconds", 1, 120, 20
        ),
        "interval_seconds": _bounded_number(
            value.get("interval_seconds"), "health.interval_seconds", 0.05, 5, 0.2
        ),
        "request_timeout_seconds": _bounded_number(
            value.get("request_timeout_seconds"),
            "health.request_timeout_seconds",
            0.1,
            10,
            2,
        ),
    }


def _load_spec(path: Path) -> tuple[dict[str, Any], str]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise DeploymentError(f"Cannot read deployment spec: {exc}") from exc
    if len(raw) > MAX_SPEC_BYTES:
        _fail(f"Deployment spec exceeds {MAX_SPEC_BYTES} bytes")
    try:
        value = json.loads(raw, object_pairs_hook=_pairs)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DeploymentError(f"Cannot parse deployment spec: {exc}") from exc
    if not isinstance(value, dict):
        _fail("Deployment spec must be an object")
    allowed = {
        "schema_version",
        "argv",
        "working_directory",
        "environment",
        "health",
        "stop_timeout_seconds",
    }
    unknown = set(value) - allowed
    if unknown:
        _fail(f"Unknown deployment spec field: {sorted(unknown)[0]}")
    if value.get("schema_version") != SCHEMA_VERSION:
        _fail(f"Deployment spec schema_version must equal {SCHEMA_VERSION}")
    raw_argv = value.get("argv")
    if (
        not isinstance(raw_argv, list)
        or not 1 <= len(raw_argv) <= 100
        or any(not isinstance(item, str) or not item or "\x00" in item for item in raw_argv)
    ):
        _fail("argv must contain 1-100 nonempty strings")
    argv = [_validate_template(item) for item in raw_argv]
    raw_environment = value.get("environment", {})
    if not isinstance(raw_environment, dict) or len(raw_environment) > 100:
        _fail("environment must be a bounded object")
    environment: dict[str, str] = {}
    for name, item in raw_environment.items():
        if (
            not isinstance(name, str)
            or not _ENVIRONMENT_NAME.fullmatch(name)
            or name in _RESERVED_ENVIRONMENT
            or not isinstance(item, str)
            or "\x00" in item
        ):
            _fail(f"Invalid or reserved environment entry: {name!r}")
        environment[name] = _validate_template(item)
    spec = {
        "schema_version": SCHEMA_VERSION,
        "argv": argv,
        "working_directory": _relative_directory(value.get("working_directory", ".")),
        "environment": environment,
        "health": _validate_health(value.get("health")),
        "stop_timeout_seconds": _bounded_number(
            value.get("stop_timeout_seconds"), "stop_timeout_seconds", 1, 30, 10
        ),
    }
    return spec, hashlib.sha256(raw).hexdigest()


def _tree_manifest(root: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        info = path.lstat()
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        if path.is_symlink() or bool(getattr(info, "st_file_attributes", 0) & reparse):
            _fail(f"Release contains a link or reparse point: {relative}")
        if path.is_dir():
            continue
        if not path.is_file():
            _fail(f"Release contains a non-regular file: {relative}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        result[relative] = {"sha256": digest}
    return result


def _git_file_modes(workspace: Path, commit: str) -> dict[str, str]:
    result = _run(["git", "ls-tree", "-r", "-z", commit], workspace).stdout
    modes: dict[str, str] = {}
    for record in result.split("\x00"):
        if not record:
            continue
        metadata, separator, name = record.partition("\t")
        fields = metadata.split()
        if not separator or len(fields) != 3:
            _fail("Git returned an invalid tree record")
        mode, kind, _object_id = fields
        if kind != "blob" or mode not in {"100644", "100755"}:
            _fail(f"Release tree contains an unsupported entry: {name}")
        modes[name] = mode
    return modes


def _extract_archive(archive: Path, destination: Path) -> None:
    seen: set[str] = set()
    destination = destination.resolve(strict=True)
    with tarfile.open(archive, mode="r:") as bundle:
        for member in bundle:
            posix = PurePosixPath(member.name)
            if (
                not member.name
                or "\\" in member.name
                or posix.is_absolute()
                or any(part in ("", ".", "..") for part in posix.parts)
                or member.name in seen
            ):
                _fail("Git archive contains an unsafe or duplicate path")
            seen.add(member.name)
            try:
                target = destination.joinpath(*posix.parts).resolve()
            except (OSError, RuntimeError) as exc:
                raise DeploymentError(
                    f"Cannot resolve archive member: {member.name}: {exc}"
                ) from exc
            if not target.is_relative_to(destination):
                _fail("Git archive member escaped the release directory")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                source = bundle.extractfile(member)
                if source is None:
                    _fail(f"Cannot read archive member: {member.name}")
                with source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)
            else:
                _fail(f"Git archive contains an unsupported entry: {member.name}")


def _make_read_only(root: Path, modes: Mapping[str, str]) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_file():
            executable = modes.get(path.relative_to(root).as_posix()) == "100755"
            path.chmod(0o555 if executable else 0o444)
        elif path.is_dir():
            path.chmod(0o555)
    root.chmod(0o555)


def _verify_release(state: Path, commit: str) -> Path:
    unresolved_release = state / "releases" / commit
    manifest_path = state / "manifests" / f"{commit}.json"
    try:
        if unresolved_release.is_symlink():
            _fail("Release artifact cannot be a symbolic link")
        release = unresolved_release.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise DeploymentError(f"Release artifact is missing: {commit}: {exc}") from exc
    expected = _read_json(manifest_path, maximum=10 * 1024 * 1024)
    if expected.get("schema_version") != SCHEMA_VERSION or expected.get("commit") != commit:
        _fail("Release manifest identity is invalid")
    if _tree_manifest(release) != expected.get("files"):
        _fail("Immutable release artifact differs from its manifest")
    return release


def _materialize(workspace: Path, state: Path, commit: str) -> Path:
    target = state / "releases" / commit
    manifest_path = state / "manifests" / f"{commit}.json"
    if target.exists() or manifest_path.exists():
        if not target.exists() or not manifest_path.exists():
            _fail("Release artifact and manifest are inconsistent")
        return _verify_release(state, commit)
    modes = _git_file_modes(workspace, commit)
    target.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    staging = target.parent / f".{commit}-{secrets.token_hex(8)}"
    staging.mkdir()
    archive = state / f".archive-{secrets.token_hex(8)}.tar"
    try:
        with archive.open("wb") as stream:
            result = subprocess.run(
                ["git", "archive", "--format=tar", commit],
                cwd=workspace,
                stdin=subprocess.DEVNULL,
                stdout=stream,
                stderr=subprocess.PIPE,
                shell=False,
                timeout=GIT_TIMEOUT_SECONDS,
                check=False,
            )
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", "replace").strip() or "no diagnostic"
            _fail(f"git archive failed: {detail}")
        _extract_archive(archive, staging)
        files = _tree_manifest(staging)
        if set(files) != set(modes):
            _fail("Materialized release does not equal the Git tree")
        _make_read_only(staging, modes)
        staging.replace(target)
        _atomic_json(
            manifest_path,
            {
                "schema_version": SCHEMA_VERSION,
                "commit": commit,
                "created_at": datetime.now(UTC).isoformat(),
                "files": files,
                "git_modes": modes,
            },
        )
    finally:
        archive.unlink(missing_ok=True)
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    return _verify_release(state, commit)


def _linux_identity(pid: int) -> dict[str, Any] | None:
    process = Path("/proc") / str(pid)
    if not process.exists():
        return None
    try:
        raw = (process / "stat").read_text(encoding="ascii")
        closing = raw.rfind(")")
        fields = raw[closing + 2 :].split()
        birth = fields[19]
        executable = str((process / "exe").resolve(strict=True))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, IndexError, ValueError) as exc:
        raise DeploymentError(f"Cannot inspect process {pid}: {exc}") from exc
    return {"pid": pid, "birth": birth, "executable": executable}


def _windows_identity(pid: int) -> dict[str, Any] | None:
    if sys.platform != "win32":
        _fail("Windows process inspection requires a Windows host")
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    open_process.restype = wintypes.HANDLE
    get_process_times = kernel32.GetProcessTimes
    get_process_times.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    get_process_times.restype = wintypes.BOOL
    query_image = kernel32.QueryFullProcessImageNameW
    query_image.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        wintypes.LPDWORD,
    ]
    query_image.restype = wintypes.BOOL
    wait_for_process = kernel32.WaitForSingleObject
    wait_for_process.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    wait_for_process.restype = wintypes.DWORD
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    # PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE.  The wait permission lets
    # a failed metadata query distinguish a real access error from an exit race.
    handle = open_process(0x101000, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        if error == 87:
            return None
        _fail(f"Cannot inspect process {pid}: Windows error {error}")

    def exited_during_query(timeout_ms: int = 0) -> bool:
        wait_result = wait_for_process(handle, timeout_ms)
        if wait_result == 0:  # WAIT_OBJECT_0
            return True
        if wait_result == 258:  # WAIT_TIMEOUT
            return False
        _fail(f"Cannot inspect process {pid}: Windows wait error {ctypes.get_last_error()}")

    try:
        created = wintypes.FILETIME()
        exited = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        if not get_process_times(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            error = ctypes.get_last_error()
            if error in {5, 31} and exited_during_query(250):
                return None
            if exited_during_query():
                return None
            _fail(f"Cannot read process creation time: Windows error {error}")
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not query_image(handle, 0, buffer, ctypes.byref(size)):
            error = ctypes.get_last_error()
            if error in {5, 31} and exited_during_query(250):
                return None
            if exited_during_query():
                return None
            _fail(f"Cannot read process executable: Windows error {error}")
        birth = (created.dwHighDateTime << 32) | created.dwLowDateTime
        executable = os.path.normcase(os.path.realpath(buffer.value))
        return {"pid": pid, "birth": str(birth), "executable": executable}
    finally:
        close_handle(handle)


def _process_identity(pid: int) -> dict[str, Any] | None:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        _fail("Process ID is invalid")
    if sys.platform == "win32":
        return _windows_identity(pid)
    if sys.platform.startswith("linux"):
        return _linux_identity(pid)
    _fail("Managed deployment supports Windows and Linux hosts")


def _boot_identity() -> str:
    if sys.platform.startswith("linux"):
        try:
            value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        except (OSError, UnicodeError) as exc:
            raise DeploymentError(f"Cannot read Linux boot identity: {exc}") from exc
        if not re.fullmatch(r"[0-9a-f-]{36}", value):
            _fail("Linux boot identity is invalid")
        return f"linux:{value}"
    if sys.platform == "win32":
        from ctypes import wintypes

        class SystemTimeOfDayInformation(ctypes.Structure):
            _fields_ = [
                ("boot_time", ctypes.c_longlong),
                ("current_time", ctypes.c_longlong),
                ("time_zone_bias", ctypes.c_longlong),
                ("current_time_zone_id", wintypes.ULONG),
                ("reserved", wintypes.ULONG),
                ("boot_time_bias", ctypes.c_ulonglong),
                ("sleep_time_bias", ctypes.c_ulonglong),
            ]

        query = ctypes.WinDLL("ntdll").NtQuerySystemInformation
        query.argtypes = [
            wintypes.ULONG,
            ctypes.c_void_p,
            wintypes.ULONG,
            ctypes.POINTER(wintypes.ULONG),
        ]
        query.restype = ctypes.c_long
        value = SystemTimeOfDayInformation()
        returned = wintypes.ULONG()
        status = query(3, ctypes.byref(value), ctypes.sizeof(value), ctypes.byref(returned))
        if status != 0 or returned.value < 8 or value.boot_time <= 0:
            _fail(f"Cannot read Windows boot identity: NTSTATUS {status:#x}")
        return f"windows:{value.boot_time}"
    _fail("Managed deployment supports Windows and Linux hosts")


def _identity_matches(expected: Any) -> bool:
    if not isinstance(expected, dict):
        _fail("Recorded process identity is invalid")
    pid = expected.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool):
        _fail("Recorded process ID is invalid")
    actual = _process_identity(pid)
    if actual is None:
        return False
    if actual != expected:
        _fail(f"Process identity mismatch for PID {pid}; refusing to signal it")
    return True


def _same_birth(first: Mapping[str, Any], second: Mapping[str, Any]) -> bool:
    """Compare the PID-reuse-resistant portion while a Windows launcher settles."""
    return first.get("pid") == second.get("pid") and first.get("birth") == second.get("birth")


def _replace(value: str, commit: str, release: Path, data: Path) -> str:
    replacements = {
        "{commit}": commit,
        "{data}": str(data),
        "{python}": sys.executable,
        "{release}": str(release),
    }
    for old, new in replacements.items():
        value = value.replace(old, new)
    return value


def _launch_values(
    spec: Mapping[str, Any], commit: str, release: Path, state: Path
) -> tuple[list[str], Path, dict[str, str], dict[str, Any]]:
    data = state / "data"
    data.mkdir(parents=True, exist_ok=True)
    pycache = state / "pycache" / commit
    pycache.mkdir(parents=True, exist_ok=True)
    argv = [_replace(item, commit, release, data) for item in spec["argv"]]
    cwd = release / PurePosixPath(spec["working_directory"])
    try:
        cwd = cwd.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise DeploymentError(f"Deployment working directory is missing: {exc}") from exc
    if not cwd.is_dir() or not cwd.is_relative_to(release):
        _fail("Deployment working directory escaped the release")
    environment = {
        name: value for name, value in os.environ.items() if name in _INHERITED_ENVIRONMENT
    }
    for name, value in spec["environment"].items():
        environment[name] = _replace(value, commit, release, data)
    environment.update(
        KATYDID_DATA_DIR=str(data),
        KATYDID_RELEASE_COMMIT=commit,
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONPYCACHEPREFIX=str(pycache),
        PYTHONUNBUFFERED="1",
    )
    health = {
        **spec["health"],
        "url": _replace(spec["health"]["url"], commit, release, data),
        "expected_body": (
            _replace(spec["health"]["expected_body"], commit, release, data)
            if spec["health"]["expected_body"] is not None
            else None
        ),
        "expected_headers": {
            name: _replace(value, commit, release, data)
            for name, value in spec["health"]["expected_headers"].items()
        },
    }
    return argv, cwd, environment, health


def _probe(health: Mapping[str, Any], entry: Mapping[str, Any] | None = None) -> None:
    deadline = time.monotonic() + float(health["timeout_seconds"])
    last = "health endpoint did not respond"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while time.monotonic() < deadline:
        if entry is not None and (
            not _identity_matches(entry.get("supervisor"))
            or not _identity_matches(entry.get("child"))
        ):
            _fail("Managed application exited during health verification")
        try:
            request = urllib.request.Request(str(health["url"]), method="GET")
            with opener.open(
                request,
                timeout=max(
                    0.01,
                    min(
                        float(health["request_timeout_seconds"]),
                        deadline - time.monotonic(),
                    ),
                ),
            ) as response:
                body = response.read(MAX_HEALTH_BYTES + 1)
                if len(body) > MAX_HEALTH_BYTES:
                    _fail("Health response exceeds 64 KiB")
                text = body.decode("utf-8")
                if health["expected_body"] is not None and text != health["expected_body"]:
                    last = "health response body did not identify the active release"
                else:
                    mismatch = next(
                        (
                            name
                            for name, expected in health["expected_headers"].items()
                            if response.headers.get(name) != expected
                        ),
                        None,
                    )
                    if mismatch is None:
                        if entry is not None and (
                            not _identity_matches(entry.get("supervisor"))
                            or not _identity_matches(entry.get("child"))
                        ):
                            _fail("Managed application exited during health verification")
                        return
                    last = f"health response header {mismatch!r} did not identify the release"
        except (OSError, UnicodeError, urllib.error.URLError, TimeoutError) as exc:
            last = f"health request failed: {exc}"
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(float(health["interval_seconds"]), remaining))
    _fail(last)


def _registry(state: Path) -> dict[str, Any]:
    path = state / REGISTRY_NAME
    if not path.exists():
        return {"schema_version": SCHEMA_VERSION, "active": None, "previous_commit": None}
    value = _read_json(path, maximum=1024 * 1024)
    if value.get("schema_version") != SCHEMA_VERSION:
        _fail("Managed release registry has an unsupported schema")
    return value


def _checkpoint(state: Path, registry: dict[str, Any], status: str) -> None:
    registry["schema_version"] = SCHEMA_VERSION
    registry["status"] = status
    registry["updated_at"] = datetime.now(UTC).isoformat()
    _atomic_json(state / REGISTRY_NAME, registry)


def _wait_gone(identity: Mapping[str, Any], deadline: float) -> bool:
    while time.monotonic() < deadline:
        if _process_identity(int(identity["pid"])) is None:
            return True
        time.sleep(0.05)
    return _process_identity(int(identity["pid"])) is None


def _force_kill(identity: Mapping[str, Any], *, tree: bool) -> None:
    if not _identity_matches(identity):
        return
    pid = int(identity["pid"])
    if sys.platform == "win32":
        argv = ["taskkill", "/PID", str(pid), "/F"]
        if tree:
            argv.insert(-1, "/T")
        try:
            subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
                timeout=5,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise DeploymentError(f"Cannot stop owned Windows process {pid}: {exc}") from exc
    else:
        try:
            if tree:
                os.killpg(pid, signal.SIGKILL)
            else:
                os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _stop_owned(entry: Mapping[str, Any], timeout: float) -> None:
    supervisor = entry.get("supervisor")
    child = entry.get("child")
    token = entry.get("token")
    port = entry.get("control_port")
    if (
        not isinstance(supervisor, dict)
        or not isinstance(child, dict)
        or not isinstance(token, str)
        or len(token) != 64
        or not isinstance(port, int)
        or isinstance(port, bool)
    ):
        _fail("Recorded owned-process metadata is invalid")
    supervisor_live = _identity_matches(supervisor)
    child_live = _identity_matches(child)
    if not supervisor_live and not child_live:
        return
    if supervisor_live:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1) as control:
                control.sendall(token.encode("ascii") + b"\n")
                control.settimeout(1)
                if control.recv(64) != b"stopping\n":
                    _fail("Owned supervisor rejected the stop token")
        except OSError:
            pass
    deadline = time.monotonic() + timeout
    if child_live and not _wait_gone(child, deadline):
        _force_kill(child, tree=True)
    if supervisor_live and not _wait_gone(supervisor, deadline):
        _force_kill(supervisor, tree=False)
    final_deadline = time.monotonic() + 5
    if not _wait_gone(child, final_deadline) or not _wait_gone(supervisor, final_deadline):
        _fail("Owned process termination could not be confirmed")


def _start(
    state: Path,
    spec: Mapping[str, Any],
    spec_sha256: str,
    commit: str,
    release: Path,
) -> dict[str, Any]:
    token = secrets.token_hex(32)
    launch_id = secrets.token_hex(16)
    argv, cwd, environment, health = _launch_values(spec, commit, release, state)
    launches = state / "launches"
    launches.mkdir(parents=True, exist_ok=True)
    request = launches / f"{launch_id}.request.json"
    ready = launches / f"{launch_id}.ready.json"
    stdout = state / "logs" / f"{commit}.stdout.log"
    stderr = state / "logs" / f"{commit}.stderr.log"
    stdout.parent.mkdir(parents=True, exist_ok=True)
    _atomic_json(
        request,
        {
            "schema_version": SCHEMA_VERSION,
            "token": token,
            "argv": argv,
            "cwd": str(cwd),
            "environment": environment,
            "stdout": str(stdout),
            "stderr": str(stderr),
        },
    )
    try:
        request.chmod(0o600)
    except OSError:
        request.unlink(missing_ok=True)
        raise
    command = [
        sys.executable,
        "-m",
        "katydid.deployment",
        "_supervise",
        "--request",
        str(request),
        "--ready",
        str(ready),
    ]
    options: dict[str, Any] = {}
    if sys.platform == "win32":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        options["start_new_session"] = True
    try:
        process = subprocess.Popen(
            command,
            cwd=state,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            **options,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        request.unlink(missing_ok=True)
        raise DeploymentError(f"Cannot start deployment supervisor: {exc}") from exc
    supervisor_identity = _process_identity(process.pid)
    if supervisor_identity is None:
        _fail("Deployment supervisor exited before its identity was recorded")
    deadline = time.monotonic() + START_TIMEOUT_SECONDS
    payload: dict[str, Any] | None = None
    try:
        while time.monotonic() < deadline:
            if ready.exists():
                payload = _read_json(ready, maximum=16 * 1024)
                break
            if process.poll() is not None:
                _fail("Deployment supervisor exited before starting the application")
            time.sleep(0.05)
        if payload is None:
            _fail("Deployment supervisor did not become ready")
        ready_supervisor = payload.get("supervisor")
        supervisor_argv = payload.get("supervisor_argv")
        if (
            payload.get("token") != token
            or not isinstance(ready_supervisor, dict)
            or not isinstance(supervisor_argv, list)
            or not all(isinstance(item, str) for item in supervisor_argv)
            or any(token in item for item in supervisor_argv)
            or not (
                _same_birth(supervisor_identity, ready_supervisor)
                or (sys.platform == "win32" and payload.get("parent_pid") == process.pid)
            )
        ):
            _fail("Deployment supervisor identity handshake failed")
        supervisor_identity = ready_supervisor
        child = payload.get("child")
        if not isinstance(child, dict) or not _identity_matches(child):
            _fail("Managed application exited before its identity was recorded")
        entry = {
            "commit": commit,
            "release": str(release),
            "spec_sha256": spec_sha256,
            "token": token,
            "control_port": payload.get("control_port"),
            "boot_id": _boot_identity(),
            "supervisor": supervisor_identity,
            "child": child,
            "health": health,
            "argv_sha256": hashlib.sha256(
                json.dumps(argv, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
            "started_at": datetime.now(UTC).isoformat(),
            "stdout": str(stdout),
            "stderr": str(stderr),
        }
        return entry
    except BaseException:
        if payload is not None and isinstance(payload.get("child"), dict):
            provisional = {
                "supervisor": supervisor_identity,
                "child": payload["child"],
                "token": token,
                "control_port": payload.get("control_port"),
            }
            try:
                _stop_owned(provisional, float(spec["stop_timeout_seconds"]))
            except DeploymentError:
                pass
        elif _identity_matches(supervisor_identity):
            _force_kill(supervisor_identity, tree=True)
        raise
    finally:
        request.unlink(missing_ok=True)


def _assert_active(entry: Any, spec_sha256: str) -> dict[str, Any]:
    entry = _validate_entry(entry, spec_sha256)
    if not _identity_matches(entry.get("supervisor")) or not _identity_matches(entry.get("child")):
        _fail("Managed application is not running")
    return cast(dict[str, Any], entry)


def _validate_entry(entry: Any, spec_sha256: str) -> dict[str, Any]:
    if not isinstance(entry, dict):
        _fail("No managed application is active")
    if entry.get("spec_sha256") != spec_sha256:
        _fail("Deployment spec changed after launch; reconciliation is required")
    entry_commit = entry.get("commit")
    if not isinstance(entry_commit, str) or not _SHA.fullmatch(entry_commit):
        _fail("Active release commit is invalid")
    boot_id = entry.get("boot_id")
    if not isinstance(boot_id, str) or not boot_id.startswith(("linux:", "windows:")):
        _fail("Active release boot identity is invalid")
    return entry


def _prepare(
    workspace_value: str, commit_value: str, state_value: str, spec_value: str
) -> tuple[Path, str, Path, Path, dict[str, Any], str]:
    commit = _validate_commit(commit_value)
    workspace = _validate_workspace(Path(workspace_value), commit)
    state, spec_path = _validate_paths(workspace, Path(state_value), Path(spec_value))
    state.mkdir(parents=True, exist_ok=True)
    spec, spec_sha256 = _load_spec(spec_path)
    return workspace, commit, state, spec_path, spec, spec_sha256


def deploy(
    workspace_value: str, commit_value: str, state_value: str, spec_value: str
) -> dict[str, Any]:
    workspace, commit, state, spec_path, spec, spec_sha256 = _prepare(
        workspace_value, commit_value, state_value, spec_value
    )
    with _state_lock(state):
        release = _materialize(workspace, state, commit)
        registry = _registry(state)
        current = registry.get("active")
        previous_commit: str | None = None
        recovering = False
        if current is not None:
            active = _validate_entry(current, registry.get("spec_sha256", spec_sha256))
            active_commit = active.get("commit")
            if not isinstance(active_commit, str) or not _SHA.fullmatch(active_commit):
                _fail("Active release commit is invalid")
            _verify_release(state, active_commit)
            same_boot = active["boot_id"] == _boot_identity()
            if active_commit == commit:
                recorded_previous = registry.get("previous_commit")
                if recorded_previous is not None and (
                    not isinstance(recorded_previous, str) or not _SHA.fullmatch(recorded_previous)
                ):
                    _fail("Previous release commit is invalid")
                previous_commit = recorded_previous
                if same_boot:
                    supervisor_live = _identity_matches(active.get("supervisor"))
                    child_live = _identity_matches(active.get("child"))
                    if supervisor_live and child_live:
                        try:
                            _probe(active["health"], active)
                        except DeploymentError:
                            recovering = True
                        else:
                            return {
                                "action": "deploy",
                                "commit": commit,
                                "active_commit": commit,
                                "reused": True,
                            }
                    if supervisor_live or child_live:
                        _stop_owned(active, float(spec["stop_timeout_seconds"]))
                recovering = True
            else:
                previous_commit = active_commit
                if same_boot:
                    _stop_owned(active, float(spec["stop_timeout_seconds"]))
        registry.update(
            operation_commit=commit,
            active=None,
            previous_commit=previous_commit,
            spec=str(spec_path),
            spec_sha256=spec_sha256,
        )
        _checkpoint(state, registry, "restarting" if recovering else "starting")
        try:
            active = _start(state, spec, spec_sha256, commit, release)
            registry["active"] = active
            _checkpoint(state, registry, "probing")
            _probe(active["health"], active)
        except BaseException:
            _checkpoint(state, registry, "unhealthy")
            raise
        _verify_release(state, commit)
        _checkpoint(state, registry, "healthy")
        result = {
            "action": "deploy",
            "commit": commit,
            "active_commit": commit,
            "previous_commit": previous_commit,
            "reused": False,
        }
        if recovering:
            result["recovered"] = True
        return result


def health(
    workspace_value: str, commit_value: str, state_value: str, spec_value: str
) -> dict[str, Any]:
    _workspace, commit, state, _spec_path, _spec, spec_sha256 = _prepare(
        workspace_value, commit_value, state_value, spec_value
    )
    with _state_lock(state):
        registry = _registry(state)
        if registry.get("operation_commit") != commit:
            _fail("Health request does not match the current release operation")
        active = _assert_active(registry.get("active"), spec_sha256)
        active_commit = active.get("commit")
        if not isinstance(active_commit, str):
            _fail("Active release commit is invalid")
        _verify_release(state, active_commit)
        _probe(active["health"], active)
        _checkpoint(state, registry, "healthy")
        return {"action": "health", "commit": commit, "active_commit": active_commit}


def rollback(
    workspace_value: str, commit_value: str, state_value: str, spec_value: str
) -> dict[str, Any]:
    _workspace, commit, state, spec_path, spec, spec_sha256 = _prepare(
        workspace_value, commit_value, state_value, spec_value
    )
    with _state_lock(state):
        registry = _registry(state)
        if registry.get("operation_commit") != commit:
            _fail("Rollback request does not match the current release operation")
        if registry.get("spec_sha256") != spec_sha256:
            _fail("Deployment spec changed during the release operation")
        previous_commit = registry.get("previous_commit")
        if not isinstance(previous_commit, str) or not _SHA.fullmatch(previous_commit):
            _fail("No verified previous release is available for rollback")
        previous_release = _verify_release(state, previous_commit)
        current = registry.get("active")
        if current is not None:
            active = _validate_entry(current, spec_sha256)
            _stop_owned(active, float(spec["stop_timeout_seconds"]))
        registry["active"] = None
        _checkpoint(state, registry, "rolling-back")
        try:
            restored = _start(state, spec, spec_sha256, previous_commit, previous_release)
            registry["active"] = restored
            _checkpoint(state, registry, "probing-rollback")
            _probe(restored["health"], restored)
        except BaseException:
            _checkpoint(state, registry, "rollback-unhealthy")
            raise
        registry.update(
            previous_commit=None,
            rolled_back_from=commit,
            spec=str(spec_path),
        )
        _checkpoint(state, registry, "rolled-back")
        return {"action": "rollback", "commit": commit, "active_commit": previous_commit}


def stop(
    workspace_value: str, commit_value: str, state_value: str, spec_value: str
) -> dict[str, Any]:
    _workspace, commit, state, _spec_path, spec, spec_sha256 = _prepare(
        workspace_value, commit_value, state_value, spec_value
    )
    with _state_lock(state):
        registry = _registry(state)
        if registry.get("operation_commit") != commit:
            _fail("Stop request does not match the current release operation")
        active = _assert_active(registry.get("active"), spec_sha256)
        _stop_owned(active, float(spec["stop_timeout_seconds"]))
        registry["active"] = None
        _checkpoint(state, registry, "stopped")
        return {"action": "stop", "commit": commit, "active_commit": None}


def recover(state_value: str, spec_value: str) -> dict[str, Any]:
    unresolved_state = Path(state_value)
    unresolved_spec = Path(spec_value)
    if not unresolved_state.is_absolute() or not unresolved_spec.is_absolute():
        _fail("Recovery state and spec paths must be absolute")
    try:
        state = unresolved_state.resolve()
        spec_path = unresolved_spec.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise DeploymentError(f"Cannot resolve recovery paths: {exc}") from exc
    if not spec_path.is_file():
        _fail("Recovery needs an existing deployment spec")
    if not state.exists():
        return {
            "action": "recover",
            "active_commit": None,
            "recovered": False,
            "status": "skipped",
        }
    if not state.is_dir():
        _fail("Recovery state path is not a directory")
    spec, spec_sha256 = _load_spec(spec_path)
    with _state_lock(state):
        registry = _registry(state)
        established_status = registry.get("status")
        if established_status not in {"healthy", "rolled-back"}:
            return {
                "action": "recover",
                "active_commit": None,
                "recovered": False,
                "status": "skipped",
            }
        if registry.get("spec") != str(spec_path) or registry.get("spec_sha256") != spec_sha256:
            _fail("Deployment spec changed after launch; reconciliation is required")
        active = _validate_entry(registry.get("active"), spec_sha256)
        active_commit = cast(str, active["commit"])
        release = _verify_release(state, active_commit)
        same_boot = active["boot_id"] == _boot_identity()
        if same_boot:
            supervisor_live = _identity_matches(active.get("supervisor"))
            child_live = _identity_matches(active.get("child"))
            if supervisor_live and child_live:
                try:
                    _probe(active["health"], active)
                except DeploymentError:
                    pass
                else:
                    return {
                        "action": "recover",
                        "active_commit": active_commit,
                        "recovered": False,
                        "status": "healthy",
                    }
            if supervisor_live or child_live:
                _stop_owned(active, float(spec["stop_timeout_seconds"]))
        restored = _start(state, spec, spec_sha256, active_commit, release)
        registry["active"] = restored
        _checkpoint(state, registry, cast(str, established_status))
        try:
            _probe(restored["health"], restored)
        except BaseException:
            try:
                _stop_owned(restored, float(spec["stop_timeout_seconds"]))
            except DeploymentError:
                pass
            raise
        _verify_release(state, active_commit)
        _checkpoint(state, registry, cast(str, established_status))
        return {
            "action": "recover",
            "active_commit": active_commit,
            "recovered": True,
            "status": "healthy",
        }


def _stop_child(process: subprocess.Popen[bytes], identity: Mapping[str, Any]) -> None:
    if process.poll() is not None:
        process.wait(timeout=5)
        return
    if not _identity_matches(identity):
        _fail("Supervisor child identity changed")
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            timeout=5,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            if _identity_matches(identity):
                _force_kill(identity, tree=True)
    process.wait(timeout=5)


def _supervise(request_value: str, ready_value: str) -> int:
    request = Path(request_value).resolve(strict=True)
    ready = Path(ready_value).resolve()
    config = _read_json(request, maximum=1024 * 1024)
    required = {
        "schema_version",
        "token",
        "argv",
        "cwd",
        "environment",
        "stdout",
        "stderr",
    }
    if set(config) != required or config.get("schema_version") != SCHEMA_VERSION:
        _fail("Supervisor launch request is invalid")
    token = config["token"]
    if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{64}", token):
        _fail("Supervisor token is invalid")
    argv = config["argv"]
    environment = config["environment"]
    if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
        _fail("Supervisor argv is invalid")
    if not isinstance(environment, dict) or "KATYDID_DEPLOYMENT_TOKEN" in environment:
        _fail("Supervisor environment is invalid")
    cwd = Path(config["cwd"]).resolve(strict=True)
    stdout = Path(config["stdout"]).resolve()
    stderr = Path(config["stderr"]).resolve()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(0.25)
    child_options: dict[str, Any] = {}
    if sys.platform == "win32":
        child_options["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        )
    else:
        child_options["start_new_session"] = True
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        env={str(name): str(value) for name, value in environment.items()},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        shell=False,
        **child_options,
    )
    if process.stdout is None or process.stderr is None:
        process.kill()
        _fail("Cannot create managed application log pipes")
    drains = [
        threading.Thread(
            target=_drain_rotating,
            args=(process.stdout, stdout),
            name="katydid-stdout",
            daemon=True,
        ),
        threading.Thread(
            target=_drain_rotating,
            args=(process.stderr, stderr),
            name="katydid-stderr",
            daemon=True,
        ),
    ]
    started_drains: list[threading.Thread] = []
    child_identity: dict[str, Any] | None = None
    try:
        for drain in drains:
            drain.start()
            started_drains.append(drain)
        child_identity = _process_identity(process.pid)
        supervisor_identity = _process_identity(os.getpid())
        if child_identity is None or supervisor_identity is None:
            process.kill()
            _fail("Cannot record managed process identities")
        _atomic_json(
            ready,
            {
                "schema_version": SCHEMA_VERSION,
                "token": token,
                "control_port": listener.getsockname()[1],
                "parent_pid": os.getppid(),
                "supervisor_argv": sys.argv,
                "supervisor": supervisor_identity,
                "child": child_identity,
            },
        )
        request.unlink(missing_ok=True)
        stopping = False
        while process.poll() is None and not stopping:
            try:
                connection, _address = listener.accept()
            except TimeoutError:
                continue
            with connection:
                connection.settimeout(1)
                supplied = connection.recv(128).strip().decode("ascii", "replace")
                if secrets.compare_digest(supplied, token):
                    connection.sendall(b"stopping\n")
                    stopping = True
                else:
                    connection.sendall(b"denied\n")
        if stopping:
            _stop_child(process, child_identity)
        else:
            process.wait(timeout=5)
    finally:
        if process.poll() is None:
            try:
                if child_identity is not None:
                    _stop_child(process, child_identity)
                else:
                    process.kill()
                    process.wait(timeout=5)
            except (DeploymentError, OSError, subprocess.SubprocessError):
                process.kill()
        deadline = time.monotonic() + LOG_DRAIN_TIMEOUT_SECONDS
        for drain in started_drains:
            drain.join(timeout=max(0.0, deadline - time.monotonic()))
        listener.close()
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    for name in ("deploy", "health", "rollback", "stop"):
        action = subparsers.add_parser(name)
        action.add_argument("--workspace", required=True)
        action.add_argument("--commit", required=True)
        action.add_argument("--state", required=True)
        action.add_argument("--spec", required=True)
    recovery = subparsers.add_parser("recover")
    recovery.add_argument("--state", required=True)
    recovery.add_argument("--spec", required=True)
    supervisor = subparsers.add_parser("_supervise", help=argparse.SUPPRESS)
    supervisor.add_argument("--request", required=True)
    supervisor.add_argument("--ready", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.action == "_supervise":
            return _supervise(args.request, args.ready)
        if args.action == "recover":
            result = recover(args.state, args.spec)
            print(json.dumps(result, sort_keys=True), flush=True)
            return 0
        operation = {"deploy": deploy, "health": health, "rollback": rollback, "stop": stop}[
            args.action
        ]
        result = operation(args.workspace, args.commit, args.state, args.spec)
        print(json.dumps(result, sort_keys=True), flush=True)
        return 0
    except DeploymentError as exc:
        print(f"managed release failed: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
