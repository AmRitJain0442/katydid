import json
import subprocess
import sys
import threading
import time

import pytest
import yaml

from katydid.cli import main
from katydid.evidence import Status
from katydid.profile import ProfileError, make_plan
from katydid.runner import run_plan

PASSING = '<testsuite tests="1"><testcase name="works"/></testsuite>'
FAILING = '<testsuite tests="1"><testcase name="bad"><failure/></testcase></testsuite>'


def write_report(xml):
    return (
        "from pathlib import Path; import os; "
        f"Path(os.environ['KATYDID_REPORT_PATH']).write_text({xml!r}, encoding='utf-8')"
    )


def make_profile(tmp_path, snippets, kind="test", timeout=5):
    checks = []
    for index, code in enumerate(snippets):
        script = tmp_path / f"check{index}.py"
        script.write_text(code, encoding="utf-8")
        checks.append(
            {
                "id": f"check-{index}",
                "kind": kind,
                "argv": ["{python}", str(script)],
                "timeout_seconds": timeout,
            }
        )
    path = tmp_path / "quality.yaml"
    path.write_text(
        yaml.safe_dump(
            {"schema_version": 1, "repository": "demo", "owner": "team", "checks": checks}
        ),
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (write_report(PASSING), Status.PASSED),
        (write_report(FAILING), Status.FAILED),
        ("print('zero exit without tests')", Status.MISSING_EVIDENCE),
        (write_report('<testsuite tests="0"/>'), Status.NO_TESTS),
        (
            write_report('<testsuite><testcase name="skip"><skipped/></testcase></testsuite>'),
            Status.SKIPPED,
        ),
        (write_report("bad XML"), Status.INVALID_EVIDENCE),
        (write_report(PASSING) + "; raise SystemExit(3)", Status.FAILED),
    ],
)
def test_process_success_does_not_override_test_evidence(tmp_path, code, expected):
    path = make_profile(tmp_path, [code])
    run = run_plan(make_plan(path, "pull-request"))
    assert run.results[0].status == expected
    assert run.gate.passed == (expected == Status.PASSED)
    summary = json.loads((run.directory / "run.json").read_text(encoding="utf-8"))
    assert summary["state"] == "completed"
    assert summary["gate"]["passed"] == run.gate.passed
    assert summary["plan"]["profile_sha256"]
    assert (run.directory / "000-check-0" / "stdout.log").exists()


def test_checks_continue_after_failure_and_preserve_logs(tmp_path):
    path = make_profile(
        tmp_path,
        [
            "print('first failure'); raise SystemExit(4)",
            "import sys; print('second ran', file=sys.stderr)",
        ],
        kind="command",
    )
    run = run_plan(make_plan(path, "pull-request"))
    assert [item.status for item in run.results] == [Status.FAILED, Status.PASSED]
    assert not run.gate.passed
    assert "first failure" in (run.directory / "000-check-0" / "stdout.log").read_text()
    assert "second ran" in (run.directory / "001-check-1" / "stderr.log").read_text()


def test_fresh_run_cannot_reuse_old_report(tmp_path):
    path = make_profile(tmp_path, [write_report(PASSING)])
    first = run_plan(make_plan(path, "pull-request"))
    (tmp_path / "check0.py").write_text("pass", encoding="utf-8")
    second = run_plan(make_plan(path, "pull-request"))
    assert first.directory != second.directory
    assert first.gate.passed
    assert second.results[0].status == Status.MISSING_EVIDENCE


def test_argv_handles_spaces_and_literal_shell_text(tmp_path):
    folder = tmp_path / "folder with spaces"
    folder.mkdir()
    path = make_profile(folder, ["import sys; print(sys.argv[1])"], kind="command")
    data = yaml.safe_load(path.read_text())
    data["checks"][0]["argv"].append("literal; echo $(not-a-command)")
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    run = run_plan(make_plan(path, "pull-request"))
    assert run.gate.passed
    assert (
        "literal; echo $(not-a-command)"
        in (run.directory / "000-check-0" / "stdout.log").read_text()
    )


def test_timeout_stops_foreground_process_tree(tmp_path):
    child = tmp_path / "child.py"
    marker = tmp_path / "child-survived"
    child.write_text(
        f"import time; from pathlib import Path; time.sleep(2); Path({str(marker)!r}).touch()"
    )
    code = (
        f"import subprocess,sys,time; subprocess.Popen([sys.executable,{str(child)!r}]); "
        "time.sleep(30)"
    )
    path = make_profile(tmp_path, [code], kind="command", timeout=1)
    run = run_plan(make_plan(path, "pull-request"))
    assert run.results[0].status == Status.TIMED_OUT
    assert not run.gate.passed
    time.sleep(1.5)
    assert not marker.exists(), "Child process escaped timeout cleanup"


def test_cancellation_stops_current_and_remaining_checks(tmp_path):
    path = make_profile(
        tmp_path,
        ["import time; time.sleep(30)", "raise RuntimeError('must not run')"],
        kind="command",
        timeout=35,
    )
    cancel = threading.Event()
    timer = threading.Timer(0.5, cancel.set)
    timer.start()
    try:
        run = run_plan(make_plan(path, "pull-request"), cancel=cancel)
    finally:
        timer.cancel()
        timer.join()
    assert run.cancelled
    assert not run.gate.passed
    assert all(item.status == Status.CANCELLED for item in run.results)
    assert not (run.directory / "001-check-1").exists()
    assert json.loads((run.directory / "run.json").read_text())["state"] == "cancelled"


def test_pre_cancelled_run_executes_nothing(tmp_path):
    path = make_profile(tmp_path, ["raise RuntimeError('must not run')"], kind="command")
    cancel = threading.Event()
    cancel.set()
    run = run_plan(make_plan(path, "pull-request"), cancel=cancel)
    assert run.cancelled
    assert not list(run.directory.glob("000-*"))


def test_changed_profile_is_rejected_before_execution(tmp_path):
    path = make_profile(tmp_path, ["pass"], kind="command")
    plan = make_plan(path, "pull-request")
    path.write_text(path.read_text() + "# changed\n")
    with pytest.raises(ProfileError, match="changed"):
        run_plan(plan)
    assert not (tmp_path / ".katydid").exists()


def test_launch_failure_is_reported(tmp_path):
    path = make_profile(tmp_path, ["pass"], kind="command")
    data = yaml.safe_load(path.read_text())
    data["checks"][0]["argv"] = [str(tmp_path / "does-not-exist")]
    path.write_text(yaml.safe_dump(data))
    run = run_plan(make_plan(path, "pull-request"))
    assert run.results[0].status == Status.ERROR
    assert not run.gate.passed


def test_cli_exit_codes_and_json(tmp_path, capsys):
    path = make_profile(tmp_path, [write_report(PASSING)])
    assert main(["run", str(path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["passed"]
    assert main(["cancel", result["directory"]]) == 0
    assert "already terminal" in capsys.readouterr().out
    (tmp_path / "check0.py").write_text("pass")
    assert main(["run", str(path)]) == 1
    assert not json.loads(capsys.readouterr().out)["passed"]


def test_report_placeholder_passes_absolute_path(tmp_path):
    path = make_profile(
        tmp_path,
        [f"from pathlib import Path; import sys; Path(sys.argv[1]).write_text({PASSING!r})"],
    )
    data = yaml.safe_load(path.read_text())
    data["checks"][0]["argv"].append("{report}")
    path.write_text(yaml.safe_dump(data))
    assert run_plan(make_plan(path, "pull-request")).gate.passed


def test_cancel_file_and_running_checkpoint_cannot_pass(tmp_path):
    path = make_profile(tmp_path, ["pass"], kind="command")

    def request_cancel(directory):
        summary = json.loads((directory / "run.json").read_text())
        assert summary["state"] == "running"
        assert not summary["gate"]["passed"]
        assert main(["cancel", str(directory)]) == 0

    run = run_plan(make_plan(path, "pull-request"), on_start=request_cancel)
    assert run.cancelled
    assert not run.gate.passed
    assert not list(run.directory.glob("000-*"))


def test_output_limit_blocks_a_noisy_command(tmp_path, monkeypatch):
    import katydid.runner as runner

    monkeypatch.setattr(runner, "MAX_LOG_BYTES", 1000)
    path = make_profile(tmp_path, ["print('x' * 2000)"], kind="command")
    run = run_plan(make_plan(path, "pull-request"))
    assert run.results[0].status == Status.ERROR
    assert not run.gate.passed


@pytest.mark.parametrize("data", [[], {}, {"state": "running"}])
def test_cancel_rejects_unrecognized_checkpoint(tmp_path, capsys, data):
    (tmp_path / "run.json").write_text(json.dumps(data))
    assert main(["cancel", str(tmp_path)]) == 2
    assert "recognized" in capsys.readouterr().err
    assert not (tmp_path / "cancel.request").exists()


def test_second_cli_can_cancel_a_running_cli(tmp_path):
    path = make_profile(tmp_path, ["import time; time.sleep(30)"], kind="command", timeout=35)
    output = tmp_path / "runs"
    process = subprocess.Popen(
        [sys.executable, "-m", "katydid", "run", str(path), "--output", str(output)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while True:
            checkpoints = list(output.glob("*/run.json"))
            if checkpoints:
                directory = checkpoints[0].parent
                break
            if process.poll() is not None or time.monotonic() >= deadline:
                pytest.fail("CLI did not publish its initial checkpoint")
            time.sleep(0.05)
        assert main(["cancel", str(directory)]) == 0
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 130, stderr
        assert json.loads(stdout)["cancelled"]
        summary = json.loads((directory / "run.json").read_text())
        assert summary["state"] == "cancelled"
        assert not summary["gate"]["passed"]
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
