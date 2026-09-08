"""Bounded adapters that turn real local security scanner results into safe JUnit evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath
from typing import Any, BinaryIO, Literal
from xml.etree import ElementTree

MAX_CAPTURE_BYTES = 2 * 1024 * 1024
MAX_RESULT_BYTES = 10 * 1024 * 1024
MAX_FINDINGS = 5000
MAX_TRACKED_FILES = 50000
MAX_TRACKED_FILE_BYTES = 20 * 1024 * 1024
MAX_TRACKED_TOTAL_BYTES = 1024 * 1024 * 1024
VERSIONS = {"static": "1.176.1", "dependencies": "0.74.0", "secrets": "8.30.1"}
TOOL_NAMES = {"static": "semgrep", "dependencies": "trivy", "secrets": "gitleaks"}
TOOL_VERSIONS = {TOOL_NAMES[name]: version for name, version in VERSIONS.items()}
FINDING_EXIT_CODES = {"static": 1, "dependencies": 10, "secrets": 10}
Scanner = Literal["static", "dependencies", "secrets"]


class SecurityCheckError(RuntimeError):
    """A scanner could not produce complete, trustworthy evidence."""


@dataclass(frozen=True)
class Finding:
    rule: str
    path: str
    line: int | None = None
    subject: str | None = None


@dataclass(frozen=True)
class ToolManifest:
    executable: Path
    trivy_cache: Path


@dataclass
class _Capture:
    remaining: int = MAX_CAPTURE_BYTES
    exceeded: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)

    def take(self, value: bytes) -> None:
        with self.lock:
            accepted = min(self.remaining, len(value))
            self.remaining -= accepted
            self.exceeded |= accepted != len(value)


def _drain(stream: BinaryIO, capture: _Capture) -> None:
    with stream:
        while data := stream.read(65536):
            capture.take(data)


def _safe_token(value: Any, fallback: str = "unknown") -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.:+/@-]{0,199}", value
    ):
        return fallback
    return value


def _safe_path(root: Path, value: Any) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        return "unknown"
    root = root.resolve()
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        relative = candidate.resolve().relative_to(root)
    except (OSError, ValueError):
        return "outside-scan-root"
    rendered = relative.as_posix()
    if len(rendered) > 500 or any(ord(character) < 32 for character in rendered):
        return "unreportable-path"
    return rendered


def _local_root(value: str) -> Path:
    windows = PureWindowsPath(value)
    if not value or "\x00" in value or windows.drive or windows.root or ".." in windows.parts:
        raise SecurityCheckError("scan root must be a local relative directory")
    current = Path.cwd().resolve()
    root = (current / value).resolve()
    try:
        mode = root.lstat().st_mode
    except OSError as exc:
        raise SecurityCheckError("scan root is unavailable") from exc
    if not root.is_relative_to(current) or not stat.S_ISDIR(mode) or stat.S_ISLNK(mode):
        raise SecurityCheckError("scan root must be a local regular directory")
    return root


def _local_file(root: Path, value: str, label: str) -> Path:
    windows = PureWindowsPath(value)
    if (
        not value
        or "\\" in value
        or "\x00" in value
        or windows.drive
        or windows.root
        or any(part in ("", ".", "..") for part in value.replace("\\", "/").split("/"))
    ):
        raise SecurityCheckError(f"{label} must be a relative file inside the scan root")
    root = root.resolve()
    path = (root / value).resolve()
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise SecurityCheckError(f"{label} is unavailable") from exc
    if not path.is_relative_to(root) or not stat.S_ISREG(mode) or stat.S_ISLNK(mode):
        raise SecurityCheckError(f"{label} must be a regular file inside the scan root")
    return path


def _environment(scanner: Scanner) -> dict[str, str]:
    allowed = (
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "TEMP",
        "TMP",
        "HOME",
        "USERPROFILE",
        "LOCALAPPDATA",
        "XDG_CACHE_HOME",
        "SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
    )
    result = {name: os.environ[name] for name in allowed if name in os.environ}
    result.update(
        NO_COLOR="1",
        PYTHONUNBUFFERED="1",
        SEMGREP_SEND_METRICS="off",
        SEMGREP_ENABLE_VERSION_CHECK="0",
    )
    if scanner == "secrets":
        result.pop("GITLEAKS_CONFIG", None)
        result.pop("GITLEAKS_CONFIG_TOML", None)
    return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise SecurityCheckError("security tool artifact could not be read") from exc
    return digest.hexdigest()


def _mapping(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise SecurityCheckError(f"security tool manifest has invalid {label}")
    return value


def _manifest(scanner: Scanner, value: Path | None, max_db_age_hours: int) -> ToolManifest:
    selected = value or (
        Path(os.environ["KATYDID_SECURITY_MANIFEST"])
        if os.environ.get("KATYDID_SECURITY_MANIFEST")
        else None
    )
    if selected is None:
        raise SecurityCheckError("KATYDID_SECURITY_MANIFEST or --manifest is required")
    if not selected.is_absolute():
        raise SecurityCheckError("security tool manifest path must be absolute")
    try:
        selected_mode = selected.lstat().st_mode
        if not stat.S_ISREG(selected_mode) or stat.S_ISLNK(selected_mode):
            raise SecurityCheckError("security tool manifest must be a small regular file")
        path = selected.resolve(strict=True)
        if not stat.S_ISREG(path.lstat().st_mode) or path.stat().st_size > 65536:
            raise SecurityCheckError("security tool manifest must be a small regular file")
        value_json = json.loads(path.read_bytes())
    except FileNotFoundError as exc:
        raise SecurityCheckError("security tool manifest is unavailable") from exc
    except (OSError, ValueError, RecursionError) as exc:
        raise SecurityCheckError("security tool manifest is invalid") from exc
    data = _mapping(
        value_json,
        {
            "schema_version",
            "prepared_at",
            "versions",
            "executables",
            "executable_sha256",
            "trivy_cache",
            "trivy_database",
        },
        "structure",
    )
    if data["schema_version"] != 1 or data["versions"] != TOOL_VERSIONS:
        raise SecurityCheckError("security tool manifest has unsupported versions")
    try:
        prepared = datetime.fromisoformat(data["prepared_at"])
    except (TypeError, ValueError) as exc:
        raise SecurityCheckError("security tool manifest preparation time is invalid") from exc
    if prepared.tzinfo is None:
        raise SecurityCheckError("security tool manifest preparation time is invalid")
    executables = _mapping(data["executables"], set(TOOL_VERSIONS), "executables")
    checksums = _mapping(data["executable_sha256"], set(TOOL_VERSIONS), "checksums")
    resolved: dict[str, Path] = {}
    for name in TOOL_VERSIONS:
        item = executables[name]
        expected = checksums[name]
        if (
            not isinstance(item, str)
            or not Path(item).is_absolute()
            or not isinstance(expected, str)
        ):
            raise SecurityCheckError("security tool manifest has invalid executable metadata")
        executable_path = Path(item)
        try:
            item_mode = executable_path.lstat().st_mode
            if not stat.S_ISREG(item_mode) or stat.S_ISLNK(item_mode):
                raise SecurityCheckError(
                    "security tool executable checksum does not match manifest"
                )
            executable = executable_path.resolve(strict=True)
            mode = executable.lstat().st_mode
        except OSError as exc:
            raise SecurityCheckError("security tool executable is unavailable") from exc
        if (
            not stat.S_ISREG(mode)
            or stat.S_ISLNK(mode)
            or re.fullmatch(r"[a-f0-9]{64}", expected) is None
            or _sha256(executable) != expected
        ):
            raise SecurityCheckError("security tool executable checksum does not match manifest")
        resolved[name] = executable
    cache_value = data["trivy_cache"]
    if not isinstance(cache_value, str) or not Path(cache_value).is_absolute():
        raise SecurityCheckError("security tool manifest has invalid Trivy cache")
    cache = Path(cache_value).resolve()
    try:
        cache_mode = cache.lstat().st_mode
    except OSError as exc:
        raise SecurityCheckError("Trivy cache directory is unavailable") from exc
    if not stat.S_ISDIR(cache_mode) or stat.S_ISLNK(cache_mode):
        raise SecurityCheckError("Trivy cache must be a regular directory")
    database = _mapping(
        data["trivy_database"],
        {"refreshed_at", "database_sha256", "metadata_sha256"},
        "Trivy database",
    )
    for key in ("database_sha256", "metadata_sha256"):
        if (
            not isinstance(database[key], str)
            or re.fullmatch(r"[a-f0-9]{64}", database[key]) is None
        ):
            raise SecurityCheckError("security tool manifest has invalid Trivy database hashes")
    if scanner == "dependencies":
        try:
            refreshed = datetime.fromisoformat(database["refreshed_at"])
        except (TypeError, ValueError) as exc:
            raise SecurityCheckError("Trivy database refresh time is invalid") from exc
        now = datetime.now(UTC)
        if (
            refreshed.tzinfo is None
            or refreshed > now
            or (now - refreshed).total_seconds() > max_db_age_hours * 3600
        ):
            raise SecurityCheckError("Trivy database is older than the configured maximum age")
        for filename, key in (
            ("trivy.db", "database_sha256"),
            ("metadata.json", "metadata_sha256"),
        ):
            database_file = cache / "db" / filename
            expected = database[key]
            if (
                not isinstance(expected, str)
                or re.fullmatch(r"[a-f0-9]{64}", expected) is None
                or not database_file.is_file()
                or database_file.is_symlink()
                or _sha256(database_file) != expected
            ):
                raise SecurityCheckError("Trivy database checksum does not match manifest")
    return ToolManifest(resolved[TOOL_NAMES[scanner]], cache)


def _git_names(git: str, root: Path, arguments: list[str]) -> list[str]:
    try:
        result = subprocess.run(
            [git, "-c", "core.quotepath=false", "ls-files", *arguments, "-z", "--", "."],
            cwd=root,
            env=_environment("static"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SecurityCheckError("Git file discovery failed") from exc
    if result.returncode != 0 or len(result.stdout) > 20 * 1024 * 1024:
        raise SecurityCheckError("Git file discovery failed or exceeded its limit")
    try:
        names = result.stdout.decode("utf-8").split("\x00")
    except UnicodeError as exc:
        raise SecurityCheckError("Git paths are not valid UTF-8") from exc
    if names and names[-1] == "":
        names.pop()
    return names


def _tracked_snapshot(root: Path, destination: Path, includes: list[str]) -> None:
    git = shutil.which("git")
    if git is None:
        raise SecurityCheckError("Git is required to select tracked scan inputs")
    names = _git_names(git, root, ["--cached"])
    untracked = _git_names(git, root, ["--others", "--exclude-standard"])
    ignored_directories = {".katydid", ".venv", "node_modules"}
    relevant_untracked = {
        name
        for name in untracked
        if not any(part.casefold() in ignored_directories for part in name.split("/"))
    }
    if len(includes) > 50 or len(set(includes)) != len(includes):
        raise SecurityCheckError("explicit scan inputs are duplicated or exceed 50 files")
    for name in includes:
        _local_file(root, name, "explicit scan input")
    unexpected = relevant_untracked - set(includes)
    if unexpected:
        raise SecurityCheckError("nonignored untracked files require explicit approved scan inputs")
    names.extend(name for name in includes if name not in names)
    if not names or len(names) > MAX_TRACKED_FILES or len(set(names)) != len(names):
        raise SecurityCheckError(
            "tracked scan inputs are empty, duplicated, or exceed 50,000 files"
        )
    total = 0
    for name in names:
        windows = PureWindowsPath(name)
        if (
            not name
            or "\\" in name
            or "\x00" in name
            or windows.drive
            or windows.root
            or any(part in ("", ".", "..", ".git") for part in name.split("/"))
        ):
            raise SecurityCheckError("Git returned an unsafe tracked path")
        source = root / name
        try:
            mode = source.lstat().st_mode
            size = source.stat().st_size
        except OSError as exc:
            raise SecurityCheckError("tracked scan input changed or became unavailable") from exc
        if not stat.S_ISREG(mode) or stat.S_ISLNK(mode) or size > MAX_TRACKED_FILE_BYTES:
            raise SecurityCheckError("tracked scan inputs must be bounded regular files")
        total += size
        if total > MAX_TRACKED_TOTAL_BYTES:
            raise SecurityCheckError("tracked scan inputs exceed 1 GiB")
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copyfile(source, target)
            target.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        except OSError as exc:
            raise SecurityCheckError("tracked scan input could not be copied safely") from exc


def _check_result_bound(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
        size = path.stat().st_size
    except FileNotFoundError:
        return
    except OSError as exc:
        raise SecurityCheckError("scanner result could not be inspected") from exc
    if not stat.S_ISREG(mode) or stat.S_ISLNK(mode) or size > MAX_RESULT_BYTES:
        raise SecurityCheckError("scanner result is linked or exceeded 10 MiB")


def _run(argv: list[str], root: Path, timeout: int, scanner: Scanner, result_path: Path) -> int:
    process: subprocess.Popen[bytes] | None = None
    readers: list[threading.Thread] = []
    capture = _Capture()
    started = time.monotonic()
    try:
        try:
            process = subprocess.Popen(
                argv,
                cwd=root,
                env=_environment(scanner),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise SecurityCheckError("scanner process could not start") from exc
        assert process.stdout is not None and process.stderr is not None
        readers = [
            threading.Thread(target=_drain, args=(process.stdout, capture), daemon=True),
            threading.Thread(target=_drain, args=(process.stderr, capture), daemon=True),
        ]
        for reader in readers:
            reader.start()
        while process.poll() is None:
            _check_result_bound(result_path)
            if capture.exceeded:
                raise SecurityCheckError("scanner console output exceeded 2 MiB")
            if time.monotonic() - started >= timeout:
                raise SecurityCheckError("scanner exceeded its internal timeout")
            time.sleep(0.05)
        for reader in readers:
            reader.join(timeout=5)
        _check_result_bound(result_path)
        if capture.exceeded:
            raise SecurityCheckError("scanner console output exceeded 2 MiB")
        return process.returncode
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        for reader in readers:
            reader.join(timeout=5)


def _version(scanner: Scanner, executable: str, root: Path, timeout: int) -> None:
    command = [executable, "--version"] if scanner != "secrets" else [executable, "version"]
    with tempfile.TemporaryDirectory(prefix="katydid-security-version-") as directory:
        output = Path(directory) / "version.txt"
        try:
            completed = subprocess.run(
                command,
                cwd=root,
                env=_environment(scanner),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=min(timeout, 30),
                check=False,
            )
            output.write_bytes(completed.stdout[:4096])
        except (OSError, subprocess.SubprocessError) as exc:
            raise SecurityCheckError("scanner version check failed") from exc
        version = VERSIONS[scanner]
        text = output.read_text(encoding="utf-8", errors="replace")
        if (
            completed.returncode != 0
            or re.search(rf"(?<![0-9.]){re.escape(version)}(?![0-9.])", text) is None
        ):
            raise SecurityCheckError(f"scanner version must be exactly {version}")


def _read_json(path: Path) -> Any:
    try:
        if not stat.S_ISREG(path.lstat().st_mode) or path.stat().st_size > MAX_RESULT_BYTES:
            raise SecurityCheckError("scanner result is missing, linked, or oversized")
        return json.loads(path.read_bytes())
    except FileNotFoundError as exc:
        raise SecurityCheckError("scanner result is missing, linked, or oversized") from exc
    except (OSError, ValueError, RecursionError) as exc:
        raise SecurityCheckError("scanner result is invalid JSON") from exc


def _semgrep_findings(value: Any, root: Path) -> list[Finding]:
    if not isinstance(value, dict) or not isinstance(value.get("results"), list):
        raise SecurityCheckError("Semgrep result has an invalid structure")
    errors = value.get("errors", [])
    if not isinstance(errors, list) or errors:
        raise SecurityCheckError("Semgrep reported an incomplete scan")
    findings = []
    for item in value["results"]:
        if not isinstance(item, dict):
            raise SecurityCheckError("Semgrep result has an invalid finding")
        start = item.get("start")
        line = start.get("line") if isinstance(start, dict) else None
        findings.append(
            Finding(
                _safe_token(item.get("check_id"), "unknown-rule"),
                _safe_path(root, item.get("path")),
                line if isinstance(line, int) and line > 0 else None,
            )
        )
    return findings


def _dependency_findings(value: Any, root: Path) -> list[Finding]:
    if not isinstance(value, dict) or not isinstance(value.get("Results"), list):
        raise SecurityCheckError("Trivy result has an invalid structure")
    findings = []
    for result in value["Results"]:
        if not isinstance(result, dict):
            raise SecurityCheckError("Trivy result has an invalid target")
        vulnerabilities = result.get("Vulnerabilities")
        if vulnerabilities is None:
            continue
        if not isinstance(vulnerabilities, list):
            raise SecurityCheckError("Trivy result has invalid vulnerabilities")
        target = _safe_path(root, result.get("Target"))
        for vulnerability in vulnerabilities:
            if not isinstance(vulnerability, dict):
                raise SecurityCheckError("Trivy result has an invalid vulnerability")
            identifier = _safe_token(vulnerability.get("VulnerabilityID"), "unknown-advisory")
            package = _safe_token(vulnerability.get("PkgName"), "unknown-package")
            version = _safe_token(vulnerability.get("InstalledVersion"), "unknown-version")
            findings.append(Finding(identifier, target, subject=f"{package}@{version}"))
    return findings


def _gitleaks_findings(value: Any, root: Path) -> list[Finding]:
    if not isinstance(value, list):
        raise SecurityCheckError("Gitleaks result has an invalid structure")
    findings = []
    for item in value:
        if not isinstance(item, dict):
            raise SecurityCheckError("Gitleaks result has an invalid finding")
        line = item.get("StartLine")
        findings.append(
            Finding(
                _safe_token(item.get("RuleID"), "unknown-rule"),
                _safe_path(root, item.get("File")),
                line if isinstance(line, int) and line > 0 else None,
            )
        )
    return findings


def _junit(report: Path, scanner: Scanner, findings: list[Finding], error: str | None) -> None:
    tests = max(1, len(findings))
    suite = ElementTree.Element(
        "testsuite",
        name=f"security-{scanner}",
        tests=str(tests),
        failures=str(len(findings)),
        errors="1" if error else "0",
        skipped="0",
    )
    if error:
        case = ElementTree.SubElement(suite, "testcase", name=f"{scanner}-scan")
        ElementTree.SubElement(case, "error", message=error).text = error
    elif not findings:
        ElementTree.SubElement(suite, "testcase", name=f"{scanner}-scan")
    else:
        for index, finding in enumerate(findings, 1):
            case = ElementTree.SubElement(suite, "testcase", name=f"finding-{index}")
            location = finding.path + (f":{finding.line}" if finding.line else "")
            subject = f" for {finding.subject}" if finding.subject else ""
            message = f"{finding.rule}{subject} at {location}"
            ElementTree.SubElement(case, "failure", message=message).text = message
    report.parent.mkdir(parents=True, exist_ok=True)
    temporary = report.with_name(f".{report.name}.{os.getpid()}.tmp")
    ElementTree.indent(suite)
    ElementTree.ElementTree(suite).write(temporary, encoding="utf-8", xml_declaration=True)
    os.replace(temporary, report)


def scan(
    scanner: Scanner,
    root_value: str,
    report: Path,
    timeout: int,
    config: str | None = None,
    manifest: Path | None = None,
    max_db_age_hours: int = 72,
    includes: list[str] | None = None,
) -> int:
    if not 10 <= timeout <= 900:
        raise SecurityCheckError("security timeout must be between 10 and 900 seconds")
    if not 1 <= max_db_age_hours <= 720:
        raise SecurityCheckError("maximum database age must be between 1 and 720 hours")
    root = _local_root(root_value)
    tools = _manifest(scanner, manifest, max_db_age_hours)
    executable = str(tools.executable)
    started = time.monotonic()
    _version(scanner, executable, root, timeout)
    remaining = max(1, timeout - int(time.monotonic() - started))
    with tempfile.TemporaryDirectory(prefix=f"katydid-{scanner}-") as directory:
        temporary = Path(directory)
        snapshot = temporary / "source"
        snapshot.mkdir()
        snapshot = snapshot.resolve()
        _tracked_snapshot(root, snapshot, includes or [])
        raw = temporary / "result.json"
        if scanner == "static":
            if config is None:
                raise SecurityCheckError("Semgrep requires an explicit local rule configuration")
            _local_file(root, config, "Semgrep configuration")
            rule_file = _local_file(snapshot, config, "tracked Semgrep configuration")
            argv = [
                executable,
                "scan",
                "--config",
                str(rule_file),
                "--json",
                "--output",
                str(raw),
                "--metrics=off",
                "--disable-version-check",
                "--oss-only",
                "--strict",
                "--error",
                str(snapshot),
            ]
        elif scanner == "dependencies":
            argv = [
                executable,
                "filesystem",
                "--scanners=vuln",
                "--format=json",
                "--output",
                str(raw),
                "--exit-code=10",
                "--offline-scan",
                "--skip-db-update",
                "--skip-java-db-update",
                "--skip-check-update",
                "--skip-vex-repo-update",
                "--skip-version-check",
                "--disable-telemetry",
                "--quiet",
                "--cache-dir",
                str(tools.trivy_cache),
                "--timeout",
                f"{remaining}s",
                str(snapshot),
            ]
        else:
            if config is None:
                raise SecurityCheckError("Gitleaks requires an explicit local configuration")
            _local_file(root, config, "Gitleaks configuration")
            secret_config = _local_file(snapshot, config, "tracked Gitleaks configuration")
            argv = [
                executable,
                "dir",
                str(snapshot),
                "--config",
                str(secret_config),
                "--report-format=json",
                "--report-path",
                str(raw),
                "--redact=100",
                "--no-banner",
                "--no-color",
                "--exit-code=10",
                "--max-target-megabytes=5",
                "--max-archive-depth=0",
                "--max-decode-depth=0",
                "--timeout",
                str(remaining),
            ]
        returncode = _run(argv, snapshot, remaining, scanner, raw)
        value = _read_json(raw)
        if scanner == "static":
            findings = _semgrep_findings(value, snapshot)
        elif scanner == "dependencies":
            findings = _dependency_findings(value, snapshot)
        else:
            findings = _gitleaks_findings(value, snapshot)
    if len(findings) > MAX_FINDINGS:
        raise SecurityCheckError("scanner findings exceed the 5,000 finding limit")
    finding_code = FINDING_EXIT_CODES[scanner]
    if returncode not in (0, finding_code) or (returncode == finding_code) != bool(findings):
        raise SecurityCheckError("scanner exit status disagrees with its structured result")
    _junit(report, scanner, findings, None)
    return 1 if findings else 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a bounded local security scanner")
    parser.add_argument("scanner", choices=tuple(VERSIONS))
    parser.add_argument("--root", default=".")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--max-db-age-hours", type=int, default=72)
    parser.add_argument("--config")
    parser.add_argument("--include", action="append", default=[])
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = scan(
            args.scanner,
            args.root,
            args.report,
            args.timeout,
            args.config,
            args.manifest,
            args.max_db_age_hours,
            args.include,
        )
    except SecurityCheckError as exc:
        _junit(args.report, args.scanner, [], str(exc))
        print(f"{args.scanner} security scan could not complete", file=sys.stderr)
        return 2
    print(f"{args.scanner} security scan completed")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
