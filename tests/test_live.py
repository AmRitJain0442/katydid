import json
import os
import threading
import time

import pytest

from katydid.live import MAX_LOG_BYTES, workflow_snapshot
from katydid.profile import make_plan
from katydid.runner import run_plan


def test_live_output_is_visible_before_check_finishes_and_survives_completion(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    release = tmp_path / "continue"
    (source / "check.py").write_text(
        "import time\nfrom pathlib import Path\n"
        "print('Playwright: opening order page <test>', flush=True)\n"
        f"deadline = time.monotonic() + 15\nwhile not Path({str(release)!r}).exists():\n"
        "    if time.monotonic() > deadline: raise SystemExit(1)\n"
        "    time.sleep(0.05)\n"
        "print('Order confirmation verified', flush=True)\n",
        encoding="utf-8",
    )
    profile = source / "quality.yaml"
    profile.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "repository": "orders",
                "owner": "team",
                "checks": [{"id": "browser", "kind": "command", "argv": ["{python}", "check.py"]}],
            }
        ),
        encoding="utf-8",
    )
    state = tmp_path / "state"
    task = {"id": "task-1", "epoch": 1, "state": "testing"}
    results = []
    thread = threading.Thread(
        target=lambda: results.append(
            run_plan(make_plan(profile, "pull-request"), state / "tasks/task-1/1/runs")
        )
    )
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            snapshot = workflow_snapshot(state, task, [])
            if snapshot["runs"] and snapshot["runs"][0]["steps"][0]["status"] == "running":
                key = snapshot["runs"][0]["steps"][0]["key"]
                live = workflow_snapshot(state, task, [key])
                if "opening order page" in live["logs"][key]["stdout"]["text"]:
                    break
            time.sleep(0.05)
        else:
            pytest.fail("No live process output arrived before completion")
        assert thread.is_alive()
        assert live["runs"][0]["steps"][0]["started_at"]
        assert "<test>" in live["logs"][key]["stdout"]["text"]
        assert not live["runs"][0]["gate"]["passed"]
    finally:
        release.touch()
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert results[0].gate.passed
    task["state"] = "completed"
    final = workflow_snapshot(state, task, [key])
    assert final["runs"][0]["steps"][0]["status"] == "passed"
    assert "confirmation verified" in final["logs"][key]["stdout"]["text"]


def seed_run(tmp_path):
    run = tmp_path / "tasks/task-1/1/runs" / ("a" * 32)
    folder = run / "000-browser"
    folder.mkdir(parents=True)
    data = {
        "run_id": run.name,
        "started_at": "2026-01-01",
        "state": "running",
        "plan": {"stage": "merge", "checks": [{"id": "browser", "kind": "test"}]},
        "results": [],
    }
    (run / "run.json").write_text(json.dumps(data), encoding="utf-8")
    (folder / "progress.json").write_text(
        json.dumps({"id": "browser", "status": "running"}), encoding="utf-8"
    )
    return folder, {"id": "task-1", "epoch": 1, "state": "testing"}, f"runs/{run.name}/000-browser"


def test_log_reads_are_opt_in_bounded_and_do_not_follow_links(tmp_path):
    folder, task, key = seed_run(tmp_path)
    stdout = folder / "stdout.log"
    stdout.write_bytes(b"a" * (MAX_LOG_BYTES * 2) + b"\x1b[31mLATEST\x1b[0m")
    assert workflow_snapshot(tmp_path, task, [])["logs"] == {}
    selected = workflow_snapshot(tmp_path, task, [key])["logs"][key]["stdout"]
    assert selected["truncated"]
    assert selected["text"].endswith("LATEST")
    assert len(selected["text"]) <= MAX_LOG_BYTES
    assert workflow_snapshot(tmp_path, task, ["../../secrets"])["logs"] == {}
    private = tmp_path / "private.txt"
    private.write_text("DO-NOT-EXPOSE", encoding="utf-8")
    stdout.unlink()
    os.link(private, stdout)
    assert not workflow_snapshot(tmp_path, task, [key])["logs"][key]["stdout"]["available"]
    with pytest.raises(ValueError, match="eight"):
        workflow_snapshot(tmp_path, task, [key] * 9)


def test_ended_tasks_do_not_show_stale_running_steps_or_other_epochs(tmp_path):
    folder, task, key = seed_run(tmp_path)
    task["state"] = "failed"
    snapshot = workflow_snapshot(tmp_path, task, [])
    assert snapshot["runs"][0]["steps"][0]["status"] == "interrupted"
    task["epoch"] = 2
    assert workflow_snapshot(tmp_path, task, [key])["runs"] == []
    task["epoch"] = 1
    (folder.parent / "run.json").write_text("partial checkpoint", encoding="utf-8")
    assert workflow_snapshot(tmp_path, task, [key])["runs"] == []
    task["id"] = "../private"
    with pytest.raises(ValueError, match="identity"):
        workflow_snapshot(tmp_path, task, [])


def test_symlinked_evidence_directory_is_never_read(tmp_path):
    folder, task, key = seed_run(tmp_path)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "stdout.log").write_text("private", encoding="utf-8")
    (folder / "progress.json").unlink()
    folder.rmdir()
    try:
        folder.symlink_to(foreign, target_is_directory=True)
    except OSError:
        pytest.skip("Host does not grant symlink creation")
    assert not workflow_snapshot(tmp_path, task, [key])["logs"][key]["stdout"]["available"]


def test_progress_storage_failure_does_not_prevent_environment_cleanup(tmp_path, monkeypatch):
    from katydid import runner

    marker = tmp_path / "cleaned"
    check = {"id": "noop", "kind": "command", "argv": ["{python}", "-c", "pass"]}
    profile = tmp_path / "quality.yaml"
    profile.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "repository": "orders",
                "owner": "team",
                "checks": [check],
                "environment": {
                    "prepare": [check],
                    "cleanup": [
                        {
                            **check,
                            "id": "cleanup",
                            "argv": [
                                "{python}",
                                "-c",
                                f"from pathlib import Path; Path({str(marker)!r}).touch()",
                            ],
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    original = runner._write_json

    def fail_progress(path, value):
        if path.name == "progress.json":
            raise OSError("Progress storage unavailable")
        original(path, value)

    monkeypatch.setattr(runner, "_write_json", fail_progress)
    result = run_plan(make_plan(profile, "pull-request"))
    assert result.gate.passed
    assert marker.exists()
