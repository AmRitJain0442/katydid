from dataclasses import replace

import pytest

from katydid.evidence import CheckResult, Status, evaluate_gate, read_junit
from katydid.evidence import TestEvidence as Evidence
from katydid.profile import Plan, PlannedCheck


def report(tmp_path, source):
    path = tmp_path / "junit.xml"
    path.write_text(source, encoding="utf-8")
    return read_junit(path)


@pytest.mark.parametrize(
    ("xml", "status"),
    [
        ('<testsuite tests="1"><testcase name="works"/></testsuite>', Status.PASSED),
        (
            '<testsuites><testsuite tests="1"><testcase name="works"/></testsuite></testsuites>',
            Status.PASSED,
        ),
        ('<testsuite><testcase name="bad"><failure/></testcase></testsuite>', Status.FAILED),
        ('<testsuite><testcase name="bad"><error/></testcase></testsuite>', Status.FAILED),
        ('<testsuite tests="0"/>', Status.NO_TESTS),
        ('<testsuite><testcase name="skip"><skipped/></testcase></testsuite>', Status.SKIPPED),
        ('<testsuite tests="1"/>', Status.INVALID_EVIDENCE),
        ('<testsuite tests="0"><testcase name="hidden"/></testsuite>', Status.INVALID_EVIDENCE),
        ('<testsuite errors="1"><testcase name="ok"/></testsuite>', Status.INVALID_EVIDENCE),
        ('<testsuite tests="banana"/>', Status.INVALID_EVIDENCE),
        ("<testsuite><testcase/></testsuite>", Status.INVALID_EVIDENCE),
        (
            '<testsuite><testcase name="x"><failure/><skipped/></testcase></testsuite>',
            Status.INVALID_EVIDENCE,
        ),
        ('<testsuites><testcase name="outside-suite"/></testsuites>', Status.INVALID_EVIDENCE),
        ("<something/>", Status.INVALID_EVIDENCE),
        ("<testsuite", Status.INVALID_EVIDENCE),
        (
            '<!DOCTYPE testsuite [<!ENTITY x "boom">]><testsuite>&x;</testsuite>',
            Status.INVALID_EVIDENCE,
        ),
    ],
)
def test_junit_requires_actual_consistent_evidence(tmp_path, xml, status):
    assert report(tmp_path, xml).status == status


def test_nested_suites_are_not_double_counted(tmp_path):
    result = report(
        tmp_path,
        '<testsuites tests="2"><testsuite tests="2">'
        '<testcase name="ok"/><testsuite tests="1" skipped="1">'
        '<testcase name="skip"><skipped/></testcase>'
        "</testsuite></testsuite></testsuites>",
    )
    assert result.status == Status.PASSED
    assert result.tests == 2
    assert result.skipped == 1


def test_missing_and_nonfile_reports(tmp_path):
    assert read_junit(tmp_path / "missing.xml").status == Status.MISSING_EVIDENCE
    assert read_junit(tmp_path).status == Status.INVALID_EVIDENCE


def plan():
    required = PlannedCheck("unit", "test", ("python",), ".", 30, True)
    optional = replace(required, id="advisory", required=False)
    return Plan(
        1, "repo", "team", "pull-request", ".", "quality.yaml", "hash", (required, optional), ()
    )


def passing(check_id):
    return CheckResult(
        check_id,
        Status.PASSED,
        "ok",
        exit_code=0,
        evidence=Evidence(Status.PASSED, "one test", tests=1),
    )


def test_optional_failure_is_visible_without_hiding_required_outcome():
    gate = evaluate_gate(
        plan(),
        [passing("unit"), CheckResult("advisory", Status.FAILED, "bad")],
    )
    assert gate.passed
    assert len(gate.advisories) == 1


@pytest.mark.parametrize("status", [value for value in Status if value != Status.PASSED])
def test_required_nonpassing_results_always_block(status):
    gate = evaluate_gate(
        plan(),
        [CheckResult("unit", status, "evidence"), passing("advisory")],
    )
    assert not gate.passed
    assert any("unit:" in reason for reason in gate.reasons)


def test_missing_duplicate_and_unexpected_results_block():
    assert not evaluate_gate(plan(), []).passed
    results = [passing("unit"), passing("advisory")]
    assert not evaluate_gate(plan(), results + [results[0]]).passed
    assert not evaluate_gate(plan(), results + [CheckResult("unknown", Status.PASSED, "ok")]).passed
    assert not evaluate_gate(plan(), results[:1]).passed


def test_pass_label_without_process_or_test_evidence_is_rejected():
    valid = passing("unit")
    for invalid in (
        replace(valid, evidence=None),
        replace(valid, exit_code=None),
        replace(valid, evidence=Evidence(Status.NO_TESTS, "empty")),
    ):
        assert not evaluate_gate(plan(), [invalid, passing("advisory")]).passed


def test_orphan_suite_error_does_not_pass(tmp_path):
    result = report(
        tmp_path, '<testsuite><error>setup failed</error><testcase name="unrelated"/></testsuite>'
    )
    assert result.status == Status.INVALID_EVIDENCE
