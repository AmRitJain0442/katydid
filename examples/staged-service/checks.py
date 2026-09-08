"""Stage-specific checks with direct JUnit output and no third-party dependencies."""

import sys
from pathlib import Path
from xml.sax.saxutils import escape

from app import quote


def pull_request() -> None:
    assert quote(1_000, 1) == {
        "subtotal": 1_000,
        "discount": 0,
        "tax": 180,
        "total": 1_180,
    }
    try:
        quote(-1, 1)
    except ValueError:
        return
    raise AssertionError("negative subtotals must be rejected")


def merge() -> None:
    assert quote(2_000, 5) == {
        "subtotal": 2_000,
        "discount": 200,
        "tax": 324,
        "total": 2_124,
    }


def nightly() -> None:
    for cents in range(0, 5_001, 137):
        result = quote(cents, 3)
        assert result["total"] == result["subtotal"] - result["discount"] + result["tax"]


def release() -> None:
    result = quote(9_999, 8)
    assert tuple(result) == ("subtotal", "discount", "tax", "total")
    assert all(type(value) is int and value >= 0 for value in result.values())


CHECKS = {
    "pull-request": pull_request,
    "merge": merge,
    "nightly": nightly,
    "release": release,
}


def main() -> int:
    stage, report_name = sys.argv[1:]
    try:
        CHECKS[stage]()
    except BaseException as exc:
        failure = f'<failure message="{escape(str(exc))}" type="{type(exc).__name__}"/>'
        exit_code = 1
    else:
        failure = ""
        exit_code = 0
    report = (
        f'<testsuite name="staged-service-{stage}" tests="1" failures="{exit_code}">'
        f'<testcase name="{stage}">{failure}</testcase></testsuite>'
    )
    Path(report_name).write_text(report, encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
