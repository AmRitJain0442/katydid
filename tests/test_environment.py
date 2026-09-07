import json
import threading
from pathlib import Path

import pytest
import yaml

import katydid.runner as runner_module
from katydid.evidence import Status
from katydid.profile import ProfileError, make_plan
from katydid.runner import run_plan

PASSING_JUNIT = '<testsuite tests="1"><testcase name="environment_contract"/></testsuite>'


def script(root: Path, name: str, source: str) -> Path:
    path = root / name
    path.write_text(source, encoding="utf-8")
    return path


def command(check_id: str, path: Path, *arguments: str, timeout: int = 5) -> dict:
    return {
        "id": check_id,
        "kind": "command",
        "argv": ["{python}", str(path), *arguments],
        "timeout_seconds": timeout,
    }


def check_spec(check_id: str, path: Path, *arguments: str, timeout: int = 5) -> dict:
    return {
        "id": check_id,
        "kind": "test",
        "argv": ["{python}", str(path), *arguments],
        "timeout_seconds": timeout,
    }


def profile(root: Path, checks: list[dict], environment: dict | None = None) -> Path:
    data = {
        "schema_version": 1,
        "repository": "environment-fixture",
        "owner": "platform-team",
        "checks": checks,
    }
    if environment is not None:
        data["environment"] = environment
    path = root / "quality.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def lifecycle(prepare: list[dict], cleanup: list[dict], readiness: dict | None = None) -> dict:
    value = {"prepare": prepare, "cleanup": cleanup}
    if readiness is not None:
        value["readiness"] = readiness
    return value


def test_environment_schema_plans_ordered_required_commands_and_placeholders(tmp_path):
    prepare = script(tmp_path, "prepare.py", "pass\n")
    ready = script(tmp_path, "ready.py", "pass\n")
    cleanup = script(tmp_path, "cleanup.py", "pass\n")
    main = script(tmp_path, "main.py", "pass\n")
    path = profile(
        tmp_path,
        [check_spec("main", main, "{environment}", "{run_id}")],
        lifecycle(
            [command("migrate", prepare, "{environment}"), command("seed", prepare)],
            [command("remove", cleanup, "{environment}")],
            {
                "check": command("ready", ready, "{run_id}"),
                "timeout_seconds": 3,
                "interval_seconds": 0.05,
            },
        ),
    )

    plan = make_plan(path, "pull-request")

    assert plan.environment is not None
    assert [item.id for item in plan.environment.prepare] == ["migrate", "seed"]
    assert [item.id for item in plan.environment.cleanup] == ["remove"]
    assert plan.environment.readiness is not None
    assert plan.environment.readiness.check.id == "ready"
    assert plan.environment.readiness.timeout_seconds == 3
    assert plan.environment.readiness.interval_seconds == 0.05
    assert plan.environment.readiness.max_attempts == 20
    assert "{environment}" in plan.checks[0].argv


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value["prepare"][0].update(kind="test"),
        lambda value: value["prepare"][0].update(required=False),
        lambda value: value["prepare"][0].update(stages=["pull-request"]),
        lambda value: value["cleanup"][0].update(id="prepare"),
        lambda value: value["readiness"].update(interval_seconds=0.01),
        lambda value: value["readiness"].update(timeout_seconds=0),
        lambda value: value["readiness"].update(max_attempts=0),
    ],
)
def test_environment_schema_rejects_unsafe_lifecycle_definitions(tmp_path, mutation):
    hook = script(tmp_path, "hook.py", "pass\n")
    main = script(tmp_path, "main.py", "pass\n")
    environment = lifecycle(
        [command("prepare", hook)],
        [command("cleanup", hook)],
        {"check": command("ready", hook), "timeout_seconds": 1, "interval_seconds": 0.05},
    )
    mutation(environment)
    path = profile(tmp_path, [check_spec("main", main)], environment)

    with pytest.raises(ProfileError):
        make_plan(path, "pull-request")


def test_environment_paths_and_unscoped_placeholder_fail_planning(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    hook = script(tmp_path, "hook.py", "pass\n")
    main = script(tmp_path, "main.py", "pass\n")
    unsafe = command("prepare", hook)
    unsafe["working_directory"] = f"../{outside.name}"
    path = profile(
        tmp_path,
        [check_spec("main", main)],
        lifecycle([unsafe], [command("cleanup", hook)]),
    )
    with pytest.raises(ProfileError, match="within"):
        make_plan(path, "pull-request")

    path = profile(tmp_path, [check_spec("main", main, "{environment}")])
    with pytest.raises(ProfileError, match="requires an environment"):
        make_plan(path, "pull-request")


def test_real_sqlite_lifecycle_retries_readiness_runs_assertions_and_cleans_up(tmp_path):
    migrate = script(
        tmp_path,
        "migrate.py",
        """import os, sqlite3, sys
from pathlib import Path
environment = Path(sys.argv[1])
assert environment.is_absolute()
assert environment == Path(os.environ["KATYDID_ENVIRONMENT_DIR"])
assert sys.argv[2] == os.environ["KATYDID_RUN_ID"]
connection = sqlite3.connect(environment / "fixture.db")
connection.execute("create table values_for_run(value integer not null, run_id text not null)")
connection.commit()
connection.close()
(environment / "trace.txt").write_text("migrate\\n", encoding="utf-8")
""",
    )
    seed = script(
        tmp_path,
        "seed.py",
        """import os, sqlite3
from pathlib import Path
environment = Path(os.environ["KATYDID_ENVIRONMENT_DIR"])
connection = sqlite3.connect(environment / "fixture.db")
run_id = os.environ["KATYDID_RUN_ID"]
connection.executemany("insert into values_for_run values (?, ?)", [(2, run_id), (3, run_id)])
connection.commit()
connection.close()
with (environment / "trace.txt").open("a", encoding="utf-8") as stream: stream.write("seed\\n")
""",
    )
    ready = script(
        tmp_path,
        "ready.py",
        """import sqlite3
from pathlib import Path
environment = Path(__import__("os").environ["KATYDID_ENVIRONMENT_DIR"])
counter = environment / "readiness-count.txt"
attempt = int(counter.read_text() if counter.exists() else "0") + 1
counter.write_text(str(attempt), encoding="utf-8")
with (environment / "trace.txt").open("a", encoding="utf-8") as stream:
    stream.write(f"ready-{attempt}\\n")
connection = sqlite3.connect(environment / "fixture.db")
count = connection.execute("select count(*) from values_for_run").fetchone()[0]
connection.close()
raise SystemExit(0 if attempt >= 3 and count == 2 else 1)
""",
    )
    verify = script(
        tmp_path,
        "verify.py",
        f"""import os, sqlite3
from pathlib import Path
environment = Path(os.environ["KATYDID_ENVIRONMENT_DIR"])
connection = sqlite3.connect(environment / "fixture.db")
rows = connection.execute("select value, run_id from values_for_run order by value").fetchall()
connection.close()
assert rows == [(2, os.environ["KATYDID_RUN_ID"]), (3, os.environ["KATYDID_RUN_ID"])]
with (environment / "trace.txt").open("a", encoding="utf-8") as stream: stream.write("test\\n")
Path(os.environ["KATYDID_REPORT_PATH"]).write_text({PASSING_JUNIT!r}, encoding="utf-8")
""",
    )
    remove = script(
        tmp_path,
        "remove.py",
        """import os
from pathlib import Path
environment = Path(os.environ["KATYDID_ENVIRONMENT_DIR"])
assert (environment / "fixture.db").exists()
(environment / "fixture.db").unlink()
with (environment / "trace.txt").open("a", encoding="utf-8") as stream: stream.write("remove\\n")
""",
    )
    confirm = script(
        tmp_path,
        "confirm.py",
        """import os
from pathlib import Path
environment = Path(os.environ["KATYDID_ENVIRONMENT_DIR"])
assert not (environment / "fixture.db").exists()
with (environment / "trace.txt").open("a", encoding="utf-8") as stream: stream.write("confirm\\n")
""",
    )
    path = profile(
        tmp_path,
        [check_spec("main", verify, "{environment}", "{run_id}")],
        lifecycle(
            [
                command("migrate", migrate, "{environment}", "{run_id}"),
                command("seed", seed),
            ],
            [command("remove", remove), command("confirm", confirm)],
            {
                "check": command("ready", ready),
                "timeout_seconds": 3,
                "interval_seconds": 0.05,
            },
        ),
    )

    run = run_plan(make_plan(path, "pull-request"))

    assert run.gate.passed
    assert run.environment is not None
    assert run.environment.started
    assert run.environment.ready
    assert run.environment.cleanup_complete
    assert run.environment.phase == "finished"
    assert run.environment.error is None
    assert [item.status for item in run.environment.prepare] == [Status.PASSED, Status.PASSED]
    assert [item.status for item in run.environment.readiness] == [
        Status.FAILED,
        Status.FAILED,
        Status.PASSED,
    ]
    assert [item.status for item in run.environment.cleanup] == [Status.PASSED, Status.PASSED]
    data = run.directory / "environment" / "data"
    assert Path(run.environment.directory) == data.resolve()
    assert data.is_dir()
    assert not (data / "fixture.db").exists()
    assert (data / "trace.txt").read_text(encoding="utf-8").splitlines() == [
        "migrate",
        "seed",
        "ready-1",
        "ready-2",
        "ready-3",
        "test",
        "remove",
        "confirm",
    ]
    assert (run.directory / "environment" / "prepare-000-migrate" / "stdout.log").exists()
    assert (run.directory / "environment" / "cleanup-001-confirm" / "stdout.log").exists()
    summary = json.loads((run.directory / "run.json").read_text(encoding="utf-8"))
    assert summary["environment"]["cleanup_complete"] is True
    assert summary["environment"]["ready"] is True


def test_partial_prepare_failure_blocks_tests_but_runs_all_cleanup(tmp_path):
    marker = tmp_path / "markers"
    marker.mkdir()
    first = script(
        tmp_path,
        "first.py",
        f"from pathlib import Path; Path({str(marker / 'first')!r}).touch()",
    )
    fail = script(tmp_path, "fail.py", "raise SystemExit(7)\n")
    main_marker = marker / "test-must-not-run"
    main = script(
        tmp_path,
        "main.py",
        f"from pathlib import Path; Path({str(main_marker)!r}).touch()",
    )
    clean_one = script(
        tmp_path,
        "clean_one.py",
        f"from pathlib import Path; Path({str(marker / 'cleanup-one')!r}).touch()",
    )
    clean_two = script(
        tmp_path,
        "clean_two.py",
        f"from pathlib import Path; Path({str(marker / 'cleanup-two')!r}).touch()",
    )
    path = profile(
        tmp_path,
        [check_spec("main", main)],
        lifecycle(
            [command("first", first), command("fail", fail)],
            [command("clean-one", clean_one), command("clean-two", clean_two)],
        ),
    )

    run = run_plan(make_plan(path, "pull-request"))

    assert not run.gate.passed
    assert not main_marker.exists()
    assert run.environment is not None
    assert run.environment.started
    assert not run.environment.ready
    assert run.environment.cleanup_complete
    assert [item.status for item in run.environment.prepare] == [Status.PASSED, Status.FAILED]
    assert all(item.status == Status.ERROR for item in run.results)
    assert (marker / "cleanup-one").exists()
    assert (marker / "cleanup-two").exists()


def test_readiness_deadline_retains_attempts_blocks_tests_and_cleans_up(tmp_path):
    marker = tmp_path / "markers"
    marker.mkdir()
    prepare = script(tmp_path, "prepare.py", "pass\n")
    ready = script(tmp_path, "ready.py", "raise SystemExit(1)\n")
    main_marker = marker / "test-must-not-run"
    main = script(
        tmp_path,
        "main.py",
        f"from pathlib import Path; Path({str(main_marker)!r}).touch()",
    )
    cleanup = script(
        tmp_path,
        "cleanup.py",
        f"from pathlib import Path; Path({str(marker / 'cleanup')!r}).touch()",
    )
    path = profile(
        tmp_path,
        [check_spec("main", main)],
        lifecycle(
            [command("prepare", prepare)],
            [command("cleanup", cleanup)],
            {
                "check": command("ready", ready),
                "timeout_seconds": 1,
                "interval_seconds": 0.05,
            },
        ),
    )

    run = run_plan(make_plan(path, "pull-request"))

    assert not run.gate.passed
    assert run.environment is not None
    assert not run.environment.ready
    assert len(run.environment.readiness) >= 2
    assert all(item.status == Status.FAILED for item in run.environment.readiness)
    assert all(item.status == Status.ERROR for item in run.results)
    assert not main_marker.exists()
    assert (marker / "cleanup").exists()


def test_readiness_attempt_budget_stops_fast_probe_and_records_reason(tmp_path):
    marker = tmp_path / "markers"
    marker.mkdir()
    prepare = script(tmp_path, "prepare.py", "pass\n")
    ready = script(tmp_path, "ready.py", "raise SystemExit(1)\n")
    main_marker = marker / "test-must-not-run"
    main = script(
        tmp_path,
        "main.py",
        f"from pathlib import Path; Path({str(main_marker)!r}).touch()",
    )
    cleanup = script(
        tmp_path,
        "cleanup.py",
        f"from pathlib import Path; Path({str(marker / 'cleanup')!r}).touch()",
    )
    path = profile(
        tmp_path,
        [check_spec("main", main)],
        lifecycle(
            [command("prepare", prepare)],
            [command("cleanup", cleanup)],
            {
                "check": command("ready", ready),
                "timeout_seconds": 10,
                "interval_seconds": 0.05,
                "max_attempts": 2,
            },
        ),
    )

    run = run_plan(make_plan(path, "pull-request"))

    assert not run.gate.passed
    assert run.environment is not None
    assert len(run.environment.readiness) == 2
    assert all(item.status == Status.FAILED for item in run.environment.readiness)
    assert "attempt budget" in (run.environment.error or "")
    assert run.environment.cleanup_complete
    assert (marker / "cleanup").exists()
    assert not main_marker.exists()


def test_cancelled_test_preserves_cancellation_while_cleanup_ignores_test_cancel(tmp_path):
    marker = tmp_path / "markers"
    marker.mkdir()
    prepare = script(tmp_path, "prepare.py", "pass\n")
    entered = marker / "test-entered"
    main = script(
        tmp_path,
        "main.py",
        f"""import time
from pathlib import Path
Path({str(entered)!r}).touch()
time.sleep(30)
""",
    )
    slow_cleanup = script(tmp_path, "slow_cleanup.py", "import time; time.sleep(30)\n")
    cleanup = script(
        tmp_path,
        "cleanup.py",
        f"from pathlib import Path; Path({str(marker / 'cleanup')!r}).touch()",
    )
    path = profile(
        tmp_path,
        [check_spec("main", main, timeout=35)],
        lifecycle(
            [command("prepare", prepare)],
            [
                command("slow-cleanup", slow_cleanup, timeout=1),
                command("cleanup", cleanup),
            ],
        ),
    )
    cancel = threading.Event()
    subprocess_entered = threading.Event()
    watcher_stop = threading.Event()

    def watch_marker() -> None:
        while not watcher_stop.wait(0.01):
            if entered.exists():
                subprocess_entered.set()
                return

    watcher = threading.Thread(target=watch_marker)
    watcher.start()
    results = []
    worker = threading.Thread(
        target=lambda: results.append(run_plan(make_plan(path, "pull-request"), cancel=cancel))
    )
    worker.start()
    assert subprocess_entered.wait(10)
    cancel.set()
    worker.join(timeout=10)
    watcher_stop.set()
    watcher.join(timeout=2)

    assert not worker.is_alive()
    run = results[0]
    assert run.cancelled
    assert not run.gate.passed
    assert run.results[0].status == Status.CANCELLED
    assert run.environment is not None
    assert not run.environment.cleanup_complete
    assert [item.status for item in run.environment.cleanup] == [
        Status.TIMED_OUT,
        Status.PASSED,
    ]
    assert (marker / "cleanup").exists()


def test_pre_cancelled_run_starts_no_environment_hooks(tmp_path):
    marker = tmp_path / "must-not-exist"
    hook = script(
        tmp_path,
        "hook.py",
        f"from pathlib import Path; Path({str(marker)!r}).touch()",
    )
    path = profile(
        tmp_path,
        [check_spec("main", hook)],
        lifecycle([command("prepare", hook)], [command("cleanup", hook)]),
    )
    cancel = threading.Event()
    cancel.set()

    run = run_plan(make_plan(path, "pull-request"), cancel=cancel)

    assert run.cancelled
    assert not marker.exists()
    assert run.environment is not None
    assert not run.environment.started
    assert run.environment.phase == "cancelled"
    assert run.environment.cleanup_complete
    assert run.environment.prepare == ()
    assert run.environment.cleanup == ()


def test_failed_test_outcome_is_preserved_while_cleanup_succeeds(tmp_path):
    marker = tmp_path / "cleanup"
    prepare = script(tmp_path, "prepare.py", "pass\n")
    main = script(
        tmp_path,
        "main.py",
        """import os
from pathlib import Path
report = '<testsuite tests="1"><testcase name="actual"><failure/></testcase></testsuite>'
Path(os.environ["KATYDID_REPORT_PATH"]).write_text(report, encoding="utf-8")
raise SystemExit(1)
""",
    )
    cleanup = script(
        tmp_path,
        "cleanup.py",
        f"from pathlib import Path; Path({str(marker)!r}).touch()",
    )
    path = profile(
        tmp_path,
        [check_spec("main", main)],
        lifecycle([command("prepare", prepare)], [command("cleanup", cleanup)]),
    )

    run = run_plan(make_plan(path, "pull-request"))

    assert run.results[0].status == Status.FAILED
    assert not run.gate.passed
    assert run.environment is not None
    assert run.environment.cleanup_complete
    assert run.environment.cleanup[0].status == Status.PASSED
    assert marker.exists()


def test_cleanup_failure_blocks_passing_tests_and_later_cleanup_still_runs(tmp_path):
    marker = tmp_path / "later-cleanup"
    prepare = script(tmp_path, "prepare.py", "pass\n")
    main = script(
        tmp_path,
        "main.py",
        f"""import os
from pathlib import Path
Path(os.environ["KATYDID_REPORT_PATH"]).write_text({PASSING_JUNIT!r}, encoding="utf-8")
""",
    )
    fail_cleanup = script(tmp_path, "fail_cleanup.py", "raise SystemExit(9)\n")
    later_cleanup = script(
        tmp_path,
        "later_cleanup.py",
        f"from pathlib import Path; Path({str(marker)!r}).touch()",
    )
    path = profile(
        tmp_path,
        [check_spec("main", main)],
        lifecycle(
            [command("prepare", prepare)],
            [command("fail-cleanup", fail_cleanup), command("later-cleanup", later_cleanup)],
        ),
    )

    run = run_plan(make_plan(path, "pull-request"))

    assert run.results[0].status == Status.PASSED
    assert not run.gate.passed
    assert run.environment is not None
    assert not run.environment.cleanup_complete
    assert [item.status for item in run.environment.cleanup] == [Status.FAILED, Status.PASSED]
    assert marker.exists()
    assert any("cleanup" in reason.lower() for reason in run.gate.reasons)


def test_cleanup_journal_error_still_attempts_every_hook_and_persists_nonpassing_run(
    tmp_path, monkeypatch
):
    markers = tmp_path / "markers"
    markers.mkdir()
    prepare = script(tmp_path, "prepare.py", "pass\n")
    main = script(
        tmp_path,
        "main.py",
        f"""import os
from pathlib import Path
Path(os.environ["KATYDID_REPORT_PATH"]).write_text({PASSING_JUNIT!r}, encoding="utf-8")
""",
    )
    clean_one = script(
        tmp_path,
        "clean_one.py",
        f"from pathlib import Path; Path({str(markers / 'one')!r}).touch()",
    )
    clean_two = script(
        tmp_path,
        "clean_two.py",
        f"from pathlib import Path; Path({str(markers / 'two')!r}).touch()",
    )
    path = profile(
        tmp_path,
        [check_spec("main", main)],
        lifecycle(
            [command("prepare", prepare)],
            [command("clean-one", clean_one), command("clean-two", clean_two)],
        ),
    )
    original_write = runner_module._write_json
    injected = False

    def fail_cleanup_journal(destination, value):
        nonlocal injected
        if (
            not injected
            and destination.name == "environment.json"
            and isinstance(value, dict)
            and value.get("phase") == "cleaning"
        ):
            injected = True
            raise OSError("injected environment journal failure")
        original_write(destination, value)

    monkeypatch.setattr(runner_module, "_write_json", fail_cleanup_journal)
    started = []
    with pytest.raises(OSError, match="journal failure"):
        run_plan(make_plan(path, "pull-request"), on_start=started.append)

    assert injected
    assert (markers / "one").exists()
    assert (markers / "two").exists()
    summary = json.loads((started[0] / "run.json").read_text(encoding="utf-8"))
    assert summary["gate"]["passed"] is False
    assert summary["environment"]["cleanup_complete"] is True
    assert [item["status"] for item in summary["environment"]["cleanup"]] == [
        "passed",
        "passed",
    ]
    assert "journal failure" in summary["environment"]["error"]


def test_unexpected_test_exception_persists_nonpassing_checkpoint_and_runs_all_cleanup(
    tmp_path, monkeypatch
):
    marker = tmp_path / "later-cleanup"
    prepare = script(tmp_path, "prepare.py", "pass\n")
    main = script(tmp_path, "main.py", "pass\n")
    later_cleanup = script(
        tmp_path,
        "later_cleanup.py",
        f"from pathlib import Path; Path({str(marker)!r}).touch()",
    )
    missing_cleanup = tmp_path / "missing-cleanup-executable"
    path = profile(
        tmp_path,
        [check_spec("main", main)],
        lifecycle(
            [command("prepare", prepare)],
            [
                {"id": "error-cleanup", "kind": "command", "argv": [str(missing_cleanup)]},
                command("later-cleanup", later_cleanup),
            ],
        ),
    )
    original_execute = runner_module._execute

    def exploding_execute(check, *args, **kwargs):
        if check.id == "main":
            raise RuntimeError("injected unexpected execution failure")
        return original_execute(check, *args, **kwargs)

    monkeypatch.setattr(runner_module, "_execute", exploding_execute)
    started = []
    with pytest.raises(RuntimeError, match="unexpected execution failure"):
        run_plan(make_plan(path, "pull-request"), on_start=started.append)

    assert len(started) == 1
    summary = json.loads((started[0] / "run.json").read_text(encoding="utf-8"))
    assert summary["gate"]["passed"] is False
    assert summary["environment"]["cleanup_complete"] is False
    assert [item["status"] for item in summary["environment"]["cleanup"]] == [
        "error",
        "passed",
    ]
    assert marker.exists()


def test_concurrent_runs_use_separate_sqlite_data_and_both_cleanup(tmp_path):
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    prepare = script(
        tmp_path,
        "concurrent_prepare.py",
        """import os, sqlite3, sys, time
from pathlib import Path
environment, receipts = Path(sys.argv[1]), Path(sys.argv[2])
run_id = os.environ["KATYDID_RUN_ID"]
(receipts / f"entered-{run_id}").touch()
deadline = time.monotonic() + 10
while len(list(receipts.glob("entered-*"))) < 2:
    if time.monotonic() >= deadline: raise SystemExit("concurrent run barrier timed out")
    time.sleep(0.02)
connection = sqlite3.connect(environment / "fixture.db")
connection.execute("create table owner(run_id text not null)")
connection.execute("insert into owner values (?)", (run_id,))
connection.commit()
connection.close()
""",
    )
    verify = script(
        tmp_path,
        "concurrent_verify.py",
        f"""import os, sqlite3
from pathlib import Path
environment = Path(os.environ["KATYDID_ENVIRONMENT_DIR"])
connection = sqlite3.connect(environment / "fixture.db")
rows = connection.execute("select run_id from owner").fetchall()
connection.close()
assert rows == [(os.environ["KATYDID_RUN_ID"],)]
Path(os.environ["KATYDID_REPORT_PATH"]).write_text({PASSING_JUNIT!r}, encoding="utf-8")
""",
    )
    cleanup = script(
        tmp_path,
        "concurrent_cleanup.py",
        """import json, os, sys
from pathlib import Path
environment, receipts = Path(sys.argv[1]), Path(sys.argv[2])
database = environment / "fixture.db"
run_id = os.environ["KATYDID_RUN_ID"]
payload = {"environment": str(environment), "database_existed": database.exists()}
(receipts / f"receipt-{run_id}.json").write_text(json.dumps(payload), encoding="utf-8")
database.unlink()
""",
    )
    path = profile(
        tmp_path,
        [check_spec("main", verify)],
        lifecycle(
            [command("prepare", prepare, "{environment}", str(receipts), timeout=15)],
            [command("cleanup", cleanup, "{environment}", str(receipts))],
        ),
    )
    plan = make_plan(path, "pull-request")
    start = threading.Barrier(3)
    runs = []
    errors = []

    def execute() -> None:
        start.wait()
        try:
            runs.append(run_plan(plan, output=tmp_path / "runs"))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=execute) for _ in range(2)]
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join(timeout=20)

    assert not errors
    assert len(runs) == 2
    assert all(run.gate.passed for run in runs)
    environments = [run.directory / "environment" / "data" for run in runs]
    assert environments[0] != environments[1]
    assert all(not (environment / "fixture.db").exists() for environment in environments)
    receipt_data = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(receipts.glob("receipt-*.json"))
    ]
    assert len(receipt_data) == 2
    assert {item["environment"] for item in receipt_data} == {
        str(environment) for environment in environments
    }
    assert all(item["database_existed"] for item in receipt_data)
