"""Stage-specific library checks with direct, dependency-free JUnit output."""

import sys
from pathlib import Path
from xml.sax.saxutils import escape

from catalog import index, normalize_key


def pull_request() -> None:
    assert normalize_key("  Green Tea  ") == "green-tea"


def merge() -> None:
    assert index(["Green Tea", "Black Coffee"]) == {
        "green-tea": "Green Tea",
        "black-coffee": "Black Coffee",
    }


def nightly() -> None:
    for value in ("A", "two words", "Already-Normal"):
        assert normalize_key(normalize_key(value)) == normalize_key(value)


def release() -> None:
    assert sorted(index(["Stable API", "Release Candidate"])) == [
        "release-candidate",
        "stable-api",
    ]


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
        f'<testsuite name="staged-library-{stage}" tests="1" failures="{exit_code}">'
        f'<testcase name="{stage}">{failure}</testcase></testsuite>'
    )
    Path(report_name).write_text(report, encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
