"""Evidence interpretation and fail-closed aggregation, independent of process execution."""

import stat
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from xml.etree.ElementTree import Element, ParseError

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring

from katydid.profile import Plan

MAX_REPORT_BYTES = 10 * 1024 * 1024


class Status(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    MISSING_EVIDENCE = "missing_evidence"
    INVALID_EVIDENCE = "invalid_evidence"
    NO_TESTS = "no_tests"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class TestEvidence:
    status: Status
    detail: str
    tests: int = 0
    failures: int = 0
    errors: int = 0
    skipped: int = 0


@dataclass(frozen=True)
class CheckResult:
    id: str
    status: Status
    detail: str
    exit_code: int | None = None
    duration_seconds: float = 0
    evidence: TestEvidence | None = None


@dataclass(frozen=True)
class Gate:
    passed: bool
    reasons: tuple[str, ...]
    advisories: tuple[str, ...]


def _counts(element: Element) -> dict[str, int]:
    cases = list(element.iter("testcase"))
    for case in cases:
        outcomes = [case.find(tag) is not None for tag in ("failure", "error", "skipped")]
        if sum(outcomes) > 1:
            raise ValueError("A testcase has conflicting failure/error/skipped outcomes")
        if not case.get("name"):
            raise ValueError("Every testcase must have a name")
    return {
        "tests": len(cases),
        "failures": sum(case.find("failure") is not None for case in cases),
        "errors": sum(case.find("error") is not None for case in cases),
        "skipped": sum(case.find("skipped") is not None for case in cases),
    }


def read_junit(path: Path) -> TestEvidence:
    """Require real testcases and consistent counters, never trust just the summary."""
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError("JUnit evidence must be a regular file, not a symlink or directory")
        with path.open("rb") as stream:
            raw = stream.read(MAX_REPORT_BYTES + 1)
        if len(raw) > MAX_REPORT_BYTES:
            raise ValueError("JUnit report exceeds the 10 MiB limit")
        root = fromstring(raw, forbid_dtd=True, forbid_entities=True, forbid_external=True)
        if root.tag not in ("testsuite", "testsuites"):
            raise ValueError("Expected a testsuite or testsuites root")
        if root.tag == "testsuites" and root.find("testsuite") is None:
            raise ValueError("testsuites must contain a testsuite")
        for parent in root.iter():
            for child in parent:
                if child.tag == "testcase" and parent.tag != "testsuite":
                    raise ValueError("A testcase must be a direct child of a testsuite")
                if child.tag in ("failure", "error", "skipped") and parent.tag != "testcase":
                    raise ValueError("An outcome outside a testcase cannot be reconciled")
        counts = _counts(root)
        for suite in root.iter():
            if suite.tag in ("testsuite", "testsuites"):
                actual = _counts(suite)
                for field, count in actual.items():
                    declared = suite.get(field)
                    if declared is not None and int(declared) != count:
                        raise ValueError(f"JUnit {field} summary disagrees with testcase evidence")
        if counts["failures"] or counts["errors"]:
            return TestEvidence(Status.FAILED, "JUnit records failed or errored tests", **counts)
        if counts["tests"] == 0:
            return TestEvidence(Status.NO_TESTS, "JUnit contains zero testcases", **counts)
        if counts["tests"] == counts["skipped"]:
            return TestEvidence(Status.SKIPPED, "Every discovered testcase was skipped", **counts)
        return TestEvidence(Status.PASSED, "JUnit contains passing executed tests", **counts)
    except FileNotFoundError:
        return TestEvidence(Status.MISSING_EVIDENCE, "Expected JUnit report was not produced")
    except (OSError, ValueError, ParseError, DefusedXmlException, RecursionError) as exc:
        return TestEvidence(Status.INVALID_EVIDENCE, f"Cannot trust JUnit evidence: {exc}")


def evaluate_gate(plan: Plan, results: list[CheckResult]) -> Gate:
    """Reconcile the entire planned set; missing optional outcomes are execution gaps too."""
    expected = {check.id: check for check in plan.checks}
    seen: set[str] = set()
    reasons: list[str] = []
    advisories: list[str] = []
    if not expected or not any(check.required for check in plan.checks):
        reasons.append("The plan contains no required checks")
    for result in results:
        if result.id in seen:
            reasons.append(f"Duplicate result: {result.id}")
        seen.add(result.id)
        check = expected.get(result.id)
        if check is None:
            reasons.append(f"Unexpected result: {result.id}")
            continue
        if result.status == Status.PASSED:
            if result.exit_code != 0:
                reasons.append(f"{result.id}: passing status has no successful process evidence")
            if check.kind == "test":
                evidence = result.evidence
                if (
                    evidence is None
                    or evidence.status != Status.PASSED
                    or evidence.tests <= evidence.skipped
                    or evidence.failures != 0
                    or evidence.errors != 0
                ):
                    reasons.append(f"{result.id}: passing status has no passing test evidence")
        if result.status != Status.PASSED:
            message = f"{result.id}: {result.status.value}: {result.detail}"
            (reasons if check.required else advisories).append(message)
    for missing in sorted(expected.keys() - seen):
        reasons.append(f"Missing result: {missing}")
    return Gate(not reasons, tuple(reasons), tuple(advisories))
