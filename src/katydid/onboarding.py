"""Bounded, review-first discovery for an existing Git repository.

Onboarding reads committed metadata and writes candidate configuration.  It never
checks out repository files, installs dependencies, runs project commands, or grants
edit or delivery authority.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
import traceback
import unittest
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import TracebackType
from typing import Any, Literal
from urllib.parse import urlsplit
from xml.etree.ElementTree import Element, ElementTree, SubElement

import yaml

from katydid.fleet import enforce_policy, load_fleet
from katydid.profile import Profile, Stage, load_profile, make_plan

MAX_TREE_BYTES = 4 * 1024 * 1024
MAX_TREE_ENTRIES = 20_000
MAX_METADATA_BYTES = 512 * 1024
MAX_CONTEXT_FILES = 100
MAX_CONTEXT_BYTES = 96 * 1024
MAX_PROFILE_BYTES = 1024 * 1024
GIT_TIMEOUT_SECONDS = 120
STAGES: tuple[Stage, ...] = ("pull-request", "merge", "nightly", "release")
_GITHUB_PART = re.compile(r"[A-Za-z0-9_.-]+\Z")
_IDENTIFIER = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")
_ErrorTuple = tuple[type[BaseException], BaseException, TracebackType] | tuple[None, None, None]
_TEXT_SUFFIXES = {
    ".c",
    ".cc",
    ".conf",
    ".cpp",
    ".cs",
    ".css",
    ".go",
    ".h",
    ".hpp",
    ".html",
    ".ini",
    ".java",
    ".js",
    ".json",
    ".jsx",
    ".kt",
    ".md",
    ".mjs",
    ".php",
    ".properties",
    ".py",
    ".rb",
    ".rs",
    ".sh",
    ".sql",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
_SENSITIVE_PARTS = {
    ".aws",
    ".azure",
    ".codex",
    ".docker",
    ".git",
    ".ssh",
    ".netrc",
    ".npmrc",
}


class OnboardingError(ValueError):
    """Repository discovery or candidate generation could not finish safely."""


@dataclass(frozen=True)
class OnboardingResult:
    source: str
    inspected_commit: str
    repository_id: str
    base_branch: str
    destination: str
    profile_path: str
    fleet_path: str
    review_path: str
    checks: tuple[str, ...]
    context_paths: tuple[str, ...]
    gaps: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class _JUnitCase:
    classname: str
    name: str
    duration: float
    outcome: Literal["passed", "failure", "error", "skipped"]
    detail: str = ""


class _JUnitResult(unittest.TestResult):
    """Translate unittest result callbacks into evidence-bearing JUnit cases."""

    def __init__(self) -> None:
        super().__init__()
        self.cases: list[_JUnitCase] = []
        self.started: dict[int, float] = {}

    def startTest(self, test: unittest.case.TestCase) -> None:
        self.started[id(test)] = time.monotonic()
        super().startTest(test)

    def _record(
        self,
        test: unittest.case.TestCase,
        outcome: Literal["passed", "failure", "error", "skipped"],
        detail: str = "",
    ) -> None:
        started = self.started.get(id(test), time.monotonic())
        self.cases.append(
            _JUnitCase(
                f"{test.__class__.__module__}.{test.__class__.__qualname__}",
                str(test),
                max(0.0, time.monotonic() - started),
                outcome,
                detail[:16_384],
            )
        )

    @staticmethod
    def _trace(error: _ErrorTuple) -> str:
        if error[0] is None or error[1] is None:
            return "Test framework supplied no exception detail"
        return "".join(traceback.format_exception(error[0], error[1], error[2]))

    def addSuccess(self, test: unittest.case.TestCase) -> None:
        super().addSuccess(test)
        self._record(test, "passed")

    def addFailure(
        self,
        test: unittest.case.TestCase,
        err: _ErrorTuple,
    ) -> None:
        super().addFailure(test, err)
        self._record(test, "failure", self._trace(err))

    def addError(
        self,
        test: unittest.case.TestCase,
        err: _ErrorTuple,
    ) -> None:
        super().addError(test, err)
        self._record(test, "error", self._trace(err))

    def addSkip(self, test: unittest.case.TestCase, reason: str) -> None:
        super().addSkip(test, reason)
        self._record(test, "skipped", reason)

    def addExpectedFailure(
        self,
        test: unittest.case.TestCase,
        err: _ErrorTuple,
    ) -> None:
        super().addExpectedFailure(test, err)
        self._record(test, "skipped", f"Expected failure: {self._trace(err)}")

    def addUnexpectedSuccess(self, test: unittest.case.TestCase) -> None:
        super().addUnexpectedSuccess(test)
        self._record(test, "failure", "Unexpected success")

    def addSubTest(
        self,
        test: unittest.case.TestCase,
        subtest: unittest.case.TestCase,
        err: _ErrorTuple | None,
    ) -> None:
        super().addSubTest(test, subtest, err)
        if err is not None:
            is_failure = err[0] is not None and issubclass(err[0], test.failureException)
            outcome: Literal["failure", "error"] = "failure" if is_failure else "error"
            self._record(subtest, outcome, self._trace(err))


def _write_unittest_junit(path: Path, result: _JUnitResult, duration: float) -> None:
    failures = sum(case.outcome == "failure" for case in result.cases)
    errors = sum(case.outcome == "error" for case in result.cases)
    skipped = sum(case.outcome == "skipped" for case in result.cases)
    suite = Element(
        "testsuite",
        {
            "name": "unittest",
            "tests": str(len(result.cases)),
            "failures": str(failures),
            "errors": str(errors),
            "skipped": str(skipped),
            "time": f"{duration:.6f}",
        },
    )
    for case in result.cases:
        node = SubElement(
            suite,
            "testcase",
            {
                "classname": case.classname,
                "name": case.name,
                "time": f"{case.duration:.6f}",
            },
        )
        if case.outcome != "passed":
            child = SubElement(node, case.outcome)
            child.text = case.detail
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        ElementTree(suite).write(temporary, encoding="utf-8", xml_declaration=True)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def unittest_junit(report: Path, start_directory: str = "tests") -> int:
    """Discover unittest cases and emit actual result callbacks as bounded JUnit."""
    started = time.monotonic()
    result = _JUnitResult()
    try:
        suite = unittest.defaultTestLoader.discover(start_directory)
        suite.run(result)
    except BaseException as exc:
        result.cases.append(
            _JUnitCase(
                "katydid.onboarding",
                "unittest discovery",
                max(0.0, time.monotonic() - started),
                "error",
                "".join(traceback.format_exception(exc))[:16_384],
            )
        )
    _write_unittest_junit(report, result, time.monotonic() - started)
    failed = any(case.outcome in {"failure", "error"} for case in result.cases)
    return 0 if result.cases and not failed else 1


@dataclass(frozen=True)
class _TreeEntry:
    mode: str
    object_type: str
    object_id: str
    size: int | None
    path: str


@dataclass(frozen=True)
class _DetectedCheck:
    id: str
    kind: Literal["command", "test"]
    argv: tuple[str, ...]
    working_directory: str
    evidence: str


@dataclass
class _Discovery:
    checks: list[_DetectedCheck]
    evidence_paths: set[str]
    gaps: list[str]


class _GitTree:
    def __init__(self, repository: Path, commit: str) -> None:
        self.repository = repository
        self.commit = commit
        self.entries = self._entries()
        self.by_path = {entry.path: entry for entry in self.entries}

    def _entries(self) -> tuple[_TreeEntry, ...]:
        raw = _git(
            self.repository,
            "ls-tree",
            "-r",
            "-z",
            "-l",
            "--full-tree",
            self.commit,
            max_bytes=MAX_TREE_BYTES,
        )
        entries: list[_TreeEntry] = []
        for row in raw.split(b"\0"):
            if not row:
                continue
            try:
                header, encoded_path = row.split(b"\t", 1)
                mode, object_type, object_id, size_text = header.decode("ascii").split()
                path = encoded_path.decode("utf-8")
                size = None if size_text == "-" else int(size_text)
            except (UnicodeError, ValueError) as exc:
                raise OnboardingError("Git returned an invalid tree entry") from exc
            if not re.fullmatch(r"[0-9a-f]{40,64}", object_id):
                raise OnboardingError("Git returned an invalid object ID")
            entries.append(_TreeEntry(mode, object_type, object_id, size, path))
            if len(entries) > MAX_TREE_ENTRIES:
                raise OnboardingError(
                    f"Repository has more than {MAX_TREE_ENTRIES} committed paths"
                )
        return tuple(entries)

    def regular(self, path: str) -> _TreeEntry | None:
        entry = self.by_path.get(path)
        if entry is None:
            return None
        if entry.object_type != "blob" or entry.mode not in {"100644", "100755"}:
            raise OnboardingError(f"Metadata must be a regular committed file: {path}")
        return entry

    def read(self, path: str, *, max_bytes: int = MAX_METADATA_BYTES) -> bytes | None:
        entry = self.regular(path)
        if entry is None:
            return None
        if entry.size is None or entry.size > max_bytes:
            raise OnboardingError(f"Metadata exceeds its byte limit: {path}")
        return _git(
            self.repository,
            "cat-file",
            "blob",
            entry.object_id,
            max_bytes=max_bytes,
        )


def _run_git(argv: list[str], *, max_bytes: int = 64 * 1024) -> bytes:
    try:
        result = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OnboardingError(f"Cannot run Git: {exc}") from exc
    if len(result.stdout) > max_bytes or len(result.stderr) > 64 * 1024:
        raise OnboardingError("Git output exceeded its inspection limit")
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise OnboardingError(
            f"Git exited with code {result.returncode}: {detail or 'no diagnostic'}"
        )
    return result.stdout


def _remove_tree(path: Path) -> None:
    def remove_readonly(function: Any, value: str, _error: BaseException) -> None:
        os.chmod(value, stat.S_IWRITE)
        function(value)

    shutil.rmtree(path, onexc=remove_readonly)


def _git(repository: Path, *args: str, max_bytes: int = 64 * 1024) -> bytes:
    return _run_git(["git", "-C", str(repository), *args], max_bytes=max_bytes)


def _github_url(value: str) -> str | None:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme.lower() != "https" or parsed.hostname != "github.com":
        return None
    try:
        invalid_authority = parsed.username or parsed.password or parsed.port
    except ValueError:
        return None
    if (
        invalid_authority
        or parsed.netloc.lower() != "github.com"
        or parsed.query
        or parsed.fragment
    ):
        return None
    parts = parsed.path.removeprefix("/").split("/")
    if len(parts) != 2:
        return None
    owner, name = parts
    name = name.removesuffix(".git")
    if (
        not owner
        or not name
        or not _GITHUB_PART.fullmatch(owner)
        or not _GITHUB_PART.fullmatch(name)
    ):
        return None
    return f"https://github.com/{owner}/{name}.git"


def _validate_branch(value: str) -> str:
    if (
        not value
        or value.startswith("-")
        or ".." in value
        or "@{" in value
        or any(character in value for character in " ~^:?*[\\\x00")
    ):
        raise OnboardingError(f"Invalid base branch: {value!r}")
    return value


def _branch_and_commit(repository: Path, requested: str | None) -> tuple[str, str]:
    if requested is None:
        branch = _git(repository, "symbolic-ref", "--short", "HEAD").decode("utf-8").strip()
    else:
        branch = _validate_branch(requested)
    commit = (
        _git(
            repository,
            "rev-parse",
            "--verify",
            f"refs/heads/{branch}^{{commit}}",
        )
        .decode("ascii")
        .strip()
    )
    if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
        raise OnboardingError("Git returned an invalid commit ID")
    return branch, commit


def _local_source(source: str) -> Path:
    try:
        path = Path(source).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise OnboardingError(f"Local repository does not exist: {source}") from exc
    if not path.is_dir():
        raise OnboardingError("Local repository source must be a directory")
    top = Path(
        _git(path, "rev-parse", "--show-toplevel").decode("utf-8", errors="strict").strip()
    ).resolve()
    if top != path:
        raise OnboardingError(f"Local source must be the Git repository root: {top}")
    return path


def _portable_path(value: str) -> bool:
    if not value or "\\" in value or any(ord(character) < 32 for character in value):
        return False
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    parts = posix.parts
    if (
        posix.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or value != posix.as_posix()
        or any(part in {"", ".", ".."} for part in parts)
        or any(part.endswith((" ", ".")) or PureWindowsPath(part).is_reserved() for part in parts)
        or any(":" in part for part in parts)
    ):
        return False
    lowered = [part.casefold() for part in parts]
    name = lowered[-1]
    return not (
        any(part in _SENSITIVE_PARTS or part.startswith(".env") for part in lowered)
        or PureWindowsPath(name).suffix in {".key", ".p12", ".pem", ".pfx"}
    )


def _parse_toml(tree: _GitTree, path: str) -> dict[str, Any]:
    raw = tree.read(path)
    if raw is None:
        return {}
    try:
        value = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise OnboardingError(f"Cannot parse committed metadata {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise OnboardingError(f"Expected a table in {path}")
    return value


def _parse_json(tree: _GitTree, path: str) -> dict[str, Any]:
    raw = tree.read(path)
    if raw is None:
        return {}
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise OnboardingError(f"Cannot parse committed metadata {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise OnboardingError(f"Expected an object in {path}")
    return value


def _dependency_names(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, str):
        name = re.split(r"[\s<>=!~;\[]", value.strip(), maxsplit=1)[0]
        if name:
            found.add(name.casefold().replace("_", "-"))
    elif isinstance(value, list):
        for item in value:
            found.update(_dependency_names(item))
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                found.add(key.casefold().replace("_", "-"))
            found.update(_dependency_names(item))
    return found


def _check_id(prefix: str, directory: str, used: set[str]) -> str:
    location = re.sub(r"[^a-z0-9]+", "-", directory.casefold()).strip("-")
    base = prefix if directory == "." or not location else f"{prefix}-{location}"
    base = base[:64].rstrip("-")
    candidate = base
    counter = 2
    while candidate in used:
        suffix = f"-{counter}"
        candidate = f"{base[: 64 - len(suffix)].rstrip('-')}{suffix}"
        counter += 1
    used.add(candidate)
    return candidate


def _add_check(
    discovery: _Discovery,
    used: set[str],
    prefix: str,
    kind: Literal["command", "test"],
    argv: tuple[str, ...],
    directory: str,
    evidence: str,
) -> None:
    discovery.checks.append(
        _DetectedCheck(_check_id(prefix, directory, used), kind, argv, directory, evidence)
    )


def _discover_python(tree: _GitTree, discovery: _Discovery, used: set[str]) -> None:
    manifests = [entry.path for entry in tree.entries if entry.path.endswith("pyproject.toml")]
    for manifest in manifests:
        if not _portable_path(manifest):
            continue
        document = _parse_toml(tree, manifest)
        directory = str(PurePosixPath(manifest).parent)
        directory = "." if directory == "." else directory
        tool_value = document.get("tool")
        tool: dict[str, Any] = tool_value if isinstance(tool_value, dict) else {}
        dependencies = _dependency_names(document.get("project", {}))
        dependencies.update(_dependency_names(document.get("dependency-groups", {})))
        discovery.evidence_paths.add(manifest)
        prefix = "" if directory == "." else f"{directory}/"
        tests_exist = any(
            entry.path.startswith(f"{prefix}tests/") and entry.path.endswith(".py")
            for entry in tree.entries
        )
        if "pytest" in tool or "pytest" in dependencies:
            _add_check(
                discovery,
                used,
                "python-tests",
                "test",
                ("{python}", "-m", "pytest", "-q", "--junitxml={report}"),
                directory,
                f"{manifest}: pytest configuration or dependency",
            )
        elif tests_exist:
            _add_check(
                discovery,
                used,
                "python-tests",
                "test",
                (
                    "{python}",
                    "-I",
                    "-m",
                    "katydid.onboarding",
                    "unittest-junit",
                    "{report}",
                    "tests",
                ),
                directory,
                f"{manifest}: conventional tests directory",
            )
        else:
            discovery.gaps.append(f"{manifest}: no existing Python test command was discovered.")
        if "ruff" in tool or "ruff" in dependencies:
            _add_check(
                discovery,
                used,
                "python-lint",
                "command",
                ("{python}", "-m", "ruff", "check", "."),
                directory,
                f"{manifest}: ruff configuration or dependency",
            )
        if "mypy" in tool or "mypy" in dependencies:
            _add_check(
                discovery,
                used,
                "python-types",
                "command",
                ("{python}", "-m", "mypy", "."),
                directory,
                f"{manifest}: mypy configuration or dependency",
            )


def _discover_node(tree: _GitTree, discovery: _Discovery, used: set[str]) -> None:
    manifests = [entry.path for entry in tree.entries if entry.path.endswith("package.json")]
    for manifest in manifests:
        if not _portable_path(manifest):
            continue
        document = _parse_json(tree, manifest)
        dependencies = _dependency_names(document.get("dependencies", {}))
        dependencies.update(_dependency_names(document.get("devDependencies", {})))
        scripts = document.get("scripts")
        if not isinstance(scripts, dict):
            scripts = {}
        directory = str(PurePosixPath(manifest).parent)
        directory = "." if directory == "." else directory
        discovery.evidence_paths.add(manifest)
        test_script = scripts.get("test")
        has_test_script = (
            isinstance(test_script, str)
            and test_script.strip()
            and "no test specified" not in test_script
        )
        if has_test_script and "vitest" in dependencies:
            _add_check(
                discovery,
                used,
                "node-tests",
                "test",
                (
                    "node",
                    "node_modules/vitest/vitest.mjs",
                    "run",
                    "--reporter=junit",
                    "--outputFile={report}",
                ),
                directory,
                f"{manifest}: scripts.test and a Vitest dependency",
            )
        elif has_test_script:
            discovery.gaps.append(
                f"{manifest}: scripts.test has no verified portable JUnit contract and was omitted."
            )
        else:
            discovery.gaps.append(
                f"{manifest}: no non-placeholder package test script was discovered."
            )
        lint_script = scripts.get("lint")
        if isinstance(lint_script, str) and lint_script.strip():
            discovery.gaps.append(
                f"{manifest}: scripts.lint was not promoted because package-manager launchers "
                "are not portable native executables on Windows."
            )


def _discover_compiled(tree: _GitTree, discovery: _Discovery) -> None:
    for entry in tree.entries:
        if entry.path.endswith("Cargo.toml") and _portable_path(entry.path):
            discovery.evidence_paths.add(entry.path)
            discovery.gaps.append(
                f"{entry.path}: Cargo tests have no verified JUnit adapter and were omitted."
            )
        elif entry.path.endswith("go.mod") and _portable_path(entry.path):
            discovery.evidence_paths.add(entry.path)
            discovery.gaps.append(
                f"{entry.path}: Go tests have no verified JUnit adapter and were omitted."
            )


def _discover(tree: _GitTree, owner_supplied: bool) -> _Discovery:
    discovery = _Discovery([], set(), [])
    used: set[str] = set()
    _discover_python(tree, discovery, used)
    _discover_node(tree, discovery, used)
    _discover_compiled(tree, discovery)
    if not any(check.kind == "test" for check in discovery.checks):
        details = " ".join(discovery.gaps) or "No supported test metadata was found."
        raise OnboardingError(
            "No structured existing test command was discovered; no acceptance profile was "
            f"generated. {details}"
        )
    if not owner_supplied:
        discovery.gaps.append("The profile owner is unassigned and must be reviewed.")
    discovery.gaps.append(
        "Dependency installation, service readiness, browser setup, and external resources "
        "were not inferred."
    )
    discovery.gaps.append(
        "No editable paths, delivery authority, GitHub events, release hooks, or isolation "
        "policy were granted."
    )
    return discovery


def _context_paths(tree: _GitTree, discovery: _Discovery) -> tuple[str, ...]:
    candidates: list[tuple[int, str]] = []
    for entry in tree.entries:
        path = entry.path
        if entry.object_type != "blob" or entry.mode not in {"100644", "100755"}:
            continue
        if entry.size is None or entry.size > MAX_METADATA_BYTES or not _portable_path(path):
            continue
        pure = PurePosixPath(path)
        name = pure.name.casefold()
        suffix = pure.suffix.casefold()
        if path in discovery.evidence_paths:
            priority = 0
        elif name in {
            "cargo.lock",
            "go.sum",
            "package-lock.json",
            "pnpm-lock.yaml",
            "requirements.txt",
            "uv.lock",
            "yarn.lock",
        }:
            priority = 1
        elif "test" in {part.casefold() for part in pure.parts} or name.startswith("test_"):
            priority = 2
        elif "src" in {part.casefold() for part in pure.parts} or suffix in {
            ".go",
            ".js",
            ".py",
            ".rs",
            ".ts",
        }:
            priority = 3
        elif name.startswith("readme") or suffix in {".md", ".toml", ".yaml", ".yml"}:
            priority = 4
        else:
            continue
        candidates.append((priority, path))
    selected_list: list[str] = []
    used_bytes = 0
    for _priority, path in sorted(candidates):
        if len(selected_list) == MAX_CONTEXT_FILES:
            break
        entry = tree.by_path[path]
        assert entry.size is not None
        if entry.size > MAX_CONTEXT_BYTES - used_bytes:
            continue
        raw = tree.read(path)
        assert raw is not None
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError:
            discovery.gaps.append(f"{path}: non-UTF-8 text was excluded from model context.")
            continue
        selected_list.append(path)
        used_bytes += len(raw)
    selected = tuple(selected_list)
    if not selected:
        raise OnboardingError("No bounded nonsensitive text context was discovered")
    if len(candidates) > MAX_CONTEXT_FILES:
        discovery.gaps.append(
            f"Context was capped at {MAX_CONTEXT_FILES} of {len(candidates)} eligible "
            "committed files."
        )
    if len(selected) < len(candidates):
        discovery.gaps.append(
            f"Context selection is bounded to {MAX_CONTEXT_FILES} files and "
            f"{MAX_CONTEXT_BYTES} committed bytes."
        )
    return selected


def _profile_data(repository_id: str, owner: str, checks: list[_DetectedCheck]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "repository": repository_id,
        "owner": owner,
        "checks": [
            {
                "id": check.id,
                "kind": check.kind,
                "argv": list(check.argv),
                "working_directory": check.working_directory,
                "timeout_seconds": 600,
                "stages": list(STAGES),
                "required": True,
            }
            for check in checks
        ],
    }


def _fleet_data(
    source: str,
    repository_id: str,
    base_branch: str,
    checks: list[_DetectedCheck],
    context_paths: tuple[str, ...],
    required_checks: dict[str, Literal["command", "test"]] | None = None,
    required_checks_by_stage: dict[Stage, dict[str, Literal["command", "test"]]] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "state_directory": "state",
        "repositories": [
            {
                "id": repository_id,
                "source": source,
                "base_branch": base_branch,
                "profile": "quality.yaml",
                "context_paths": list(context_paths),
                "editable_paths": [],
                "requirements": (
                    "Review-only onboarding baseline. Run configured checks and report exact "
                    "evidence. No application edits are authorized until an operator replaces "
                    "this requirement "
                    "and grants explicit editable_paths in central policy."
                ),
                "required_checks": required_checks or {check.id: check.kind for check in checks},
                **(
                    {"required_checks_by_stage": required_checks_by_stage}
                    if required_checks_by_stage
                    else {}
                ),
                "repair_attempts": 2,
                "delivery": {"mode": "none", "auto_merge": False},
            }
        ],
    }


def _review_text(
    source: str,
    commit: str,
    repository_id: str,
    base_branch: str,
    discovery: _Discovery,
    context_paths: tuple[str, ...],
    existing_profile: bool,
) -> str:
    check_rows = "\n".join(
        f"| `{check.id}` | `{check.kind}` | `{check.working_directory}` | {check.evidence} |"
        for check in discovery.checks
    )
    context_rows = "\n".join(f"- `{path}`" for path in context_paths)
    gap_rows = "\n".join(f"- {gap}" for gap in discovery.gaps)
    activation = (
        "Confirm `quality.yaml` still matches the protected source file."
        if existing_profile
        else (
            "Copy `quality.yaml` to the repository root without replacing an existing profile, "
            "then commit it."
        )
    )
    return f"""# Onboarding review: {repository_id}

This bundle was derived from committed metadata only. Katydid did not install dependencies,
run setup code, run checks, modify the repository, or grant repair and delivery authority.

## Inspected revision

- Source: `{source}`
- Base branch: `{base_branch}`
- Commit: `{commit}`

## Required checks

The generated profile applies detected checks to all four stages. An existing profile keeps its
original stage selections; `fleet.yaml` mirrors its universal and stage-specific requirements.

| Check | Evidence kind | Working directory | Detection evidence |
| --- | --- | --- | --- |
{check_rows}

## Explicit context

{context_rows}

## Gaps requiring operator review

{gap_rows}

## Activation

1. Review each argv, working directory, stage, timeout, and gap in `quality.yaml` and this file.
2. {activation}
3. Keep `fleet.yaml` in an operator-owned directory outside the source repository.
4. Validate the installed profile at the exact source revision before registering the fleet.
5. Submit check-only tasks first. Grant exact editable paths, requirements, delivery, events,
   isolation, or release policy only through a later central-policy review.
"""


def _write_yaml(path: Path, value: dict[str, Any]) -> None:
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8", newline="\n")


def _validate_bundle(staging: Path, tree: _GitTree, stages: tuple[Stage, ...]) -> None:
    profile_path = staging / "quality.yaml"
    profile, _digest = load_profile(profile_path)
    validation_root = staging / ".validation-root"
    validation_root.mkdir()
    try:
        checks = list(profile.checks)
        if profile.environment is not None:
            checks.extend(profile.environment.prepare)
            checks.extend(profile.environment.cleanup)
            if profile.environment.readiness is not None:
                checks.append(profile.environment.readiness.check)
        working_directories = {check.working_directory for check in checks}
        for directory in working_directories:
            (validation_root / directory).mkdir(parents=True, exist_ok=True)
        if profile.isolation is not None:
            for relative in profile.isolation.files:
                tree.regular(relative)
                target = validation_root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.touch()
        fleet, _digest = load_fleet(staging / "fleet.yaml")
        repository = fleet.repositories[0]
        for stage in stages:
            plan = make_plan(profile_path, stage, validation_root)
            enforce_policy(repository, plan)
    finally:
        shutil.rmtree(validation_root)


def _identifier(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    if not normalized or not normalized[0].isalpha():
        normalized = f"repo-{normalized}".rstrip("-")
    normalized = normalized[:64].rstrip("-")
    if not _IDENTIFIER.fullmatch(normalized):
        raise OnboardingError(f"Cannot derive a repository identifier from {value!r}")
    return normalized


def _existing_profile(
    tree: _GitTree,
    staging: Path,
    repository_id: str | None,
) -> tuple[Profile | None, bytes | None]:
    raw = tree.read("quality.yaml", max_bytes=MAX_PROFILE_BYTES)
    if raw is None:
        return None, None
    path = staging / "quality.yaml"
    path.write_bytes(raw)
    profile, _digest = load_profile(path)
    if repository_id is not None and repository_id != profile.repository:
        raise OnboardingError(
            "repository_id does not match the existing protected quality.yaml profile"
        )
    return profile, raw


def _checks_from_profile(profile: Profile) -> list[_DetectedCheck]:
    return [
        _DetectedCheck(
            check.id,
            check.kind,
            tuple(check.argv),
            check.working_directory,
            "quality.yaml: existing protected check",
        )
        for check in profile.checks
    ]


def _central_requirements(
    profile: Profile,
) -> tuple[
    dict[str, Literal["command", "test"]],
    dict[Stage, dict[str, Literal["command", "test"]]],
    tuple[Stage, ...],
]:
    required = [check for check in profile.checks if check.required]
    supported = tuple(stage for stage in STAGES if any(stage in check.stages for check in required))
    common = {check.id: check.kind for check in required if set(supported).issubset(check.stages)}
    if not common:
        raise OnboardingError(
            "Existing quality.yaml needs at least one required check shared by every configured "
            "stage before it can be registered by the current central fleet schema"
        )
    by_stage: dict[Stage, dict[str, Literal["command", "test"]]] = {}
    for stage in STAGES:
        extra = {
            check.id: check.kind
            for check in required
            if stage in check.stages and check.id not in common
        }
        if extra:
            by_stage[stage] = extra
    return common, by_stage, supported


def onboard(
    source: str,
    destination: Path,
    *,
    repository_id: str | None = None,
    owner: str = "unassigned",
    base_branch: str | None = None,
) -> OnboardingResult:
    """Inspect one committed Git tree and atomically write a review bundle."""
    if not isinstance(source, str) or not source or "\x00" in source:
        raise OnboardingError("A repository source is required")
    if not isinstance(destination, Path):
        raise OnboardingError("destination must be a pathlib.Path")
    if not isinstance(owner, str) or not owner.strip() or len(owner) > 200 or "\x00" in owner:
        raise OnboardingError("owner must be a bounded nonempty string")
    github = _github_url(source)
    if github is None and ("://" in source or source.startswith(("git@", "ssh:"))):
        raise OnboardingError("Remote sources must be credential-free HTTPS GitHub URLs")

    destination = destination.expanduser().resolve()
    if destination.exists():
        raise OnboardingError(f"Destination already exists; no files were replaced: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}-", dir=destination.parent))
    remote_repository: Path | None = None
    try:
        if github is not None:
            remote_repository = staging / ".inspection.git"
            clone = [
                "git",
                "clone",
                "--bare",
                "--quiet",
                "--depth",
                "1",
                "--filter=blob:none",
                "--single-branch",
                "--no-tags",
            ]
            if base_branch is not None:
                clone.extend(["--branch", _validate_branch(base_branch)])
            clone.extend(["--", github, str(remote_repository)])
            _run_git(clone)
            repository = remote_repository
            normalized_source = github
            default_name = github.removesuffix(".git").rsplit("/", 1)[-1]
        else:
            repository = _local_source(source)
            normalized_source = str(repository)
            default_name = repository.name
            if destination.is_relative_to(repository):
                raise OnboardingError(
                    "The central onboarding destination must be outside the source"
                )

        branch, commit = _branch_and_commit(repository, base_branch)
        tree = _GitTree(repository, commit)
        existing, _profile_raw = _existing_profile(tree, staging, repository_id)
        if existing is None:
            selected_id = repository_id or _identifier(default_name)
            if not _IDENTIFIER.fullmatch(selected_id):
                raise OnboardingError("repository_id must be a lowercase Katydid identifier")
            discovery = _discover(tree, owner != "unassigned")
            required_checks = None
            required_checks_by_stage = None
            supported_stages = STAGES
            _write_yaml(
                staging / "quality.yaml", _profile_data(selected_id, owner, discovery.checks)
            )
        else:
            selected_id = existing.repository
            discovery = _Discovery(
                _checks_from_profile(existing),
                {"quality.yaml"},
                [
                    "An existing protected profile was preserved byte-for-byte; its commands, "
                    "environment hooks, and isolation settings were validated but not executed.",
                    "No editable paths, delivery authority, GitHub events, release hooks, or "
                    "central isolation policy were granted.",
                ],
            )
            required_checks, required_checks_by_stage, supported_stages = _central_requirements(
                existing
            )
            unsupported = [stage for stage in STAGES if stage not in supported_stages]
            if unsupported:
                discovery.gaps.append(
                    "Existing quality.yaml has no required checks for these stages: "
                    + ", ".join(unsupported)
                    + ". Configure them before enabling those lanes."
                )
        context_paths = _context_paths(tree, discovery)
        fleet = _fleet_data(
            normalized_source,
            selected_id,
            branch,
            discovery.checks,
            context_paths,
            required_checks,
            required_checks_by_stage,
        )
        _write_yaml(staging / "fleet.yaml", fleet)
        (staging / "ONBOARDING_REVIEW.md").write_text(
            _review_text(
                normalized_source,
                commit,
                selected_id,
                branch,
                discovery,
                context_paths,
                existing is not None,
            ),
            encoding="utf-8",
            newline="\n",
        )
        _validate_bundle(staging, tree, supported_stages)
        if remote_repository is not None:
            _remove_tree(remote_repository)
            remote_repository = None
        os.replace(staging, destination)
        return OnboardingResult(
            normalized_source,
            commit,
            selected_id,
            branch,
            str(destination),
            str(destination / "quality.yaml"),
            str(destination / "fleet.yaml"),
            str(destination / "ONBOARDING_REVIEW.md"),
            tuple(check.id for check in discovery.checks),
            context_paths,
            tuple(discovery.gaps),
        )
    except OnboardingError:
        raise
    except (OSError, UnicodeError, ValueError) as exc:
        raise OnboardingError(f"Cannot create onboarding bundle: {exc}") from exc
    finally:
        if staging.exists():
            try:
                _remove_tree(staging)
            except OSError:
                pass


def _module_main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) not in {2, 3} or arguments[0] != "unittest-junit":
        print(
            "usage: python -I -m katydid.onboarding unittest-junit REPORT [START_DIRECTORY]",
            file=sys.stderr,
        )
        return 2
    start_directory = arguments[2] if len(arguments) == 3 else "tests"
    path = PurePosixPath(start_directory)
    if (
        not start_directory
        or "\\" in start_directory
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        print("unittest start directory must be a normalized relative path", file=sys.stderr)
        return 2
    return unittest_junit(Path(arguments[1]), start_directory)


if __name__ == "__main__":
    raise SystemExit(_module_main())
