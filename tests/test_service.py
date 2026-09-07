import json
import os
import subprocess
import sys
from pathlib import Path


def cli(*args: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    for name in list(environment):
        if name.startswith("OPENAI_") or name.startswith("CODEX_"):
            environment.pop(name)
    return subprocess.run(
        [sys.executable, "-m", "katydid", *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
        timeout=60,
    )


def test_cli_initializes_validates_submits_lists_and_runs_healthy_task_without_ai(tmp_path):
    demo = tmp_path / "demo"
    initialized = cli("demo", "init", str(demo))
    assert initialized.returncode == 0, initialized.stderr
    fleet = Path(json.loads(initialized.stdout)["fleet"])
    assert fleet == demo / "fleet.yaml"
    assert (demo / "sources" / "catalog" / ".git").is_dir()

    validated = cli("fleet", str(fleet))
    assert validated.returncode == 0, validated.stderr
    validation = json.loads(validated.stdout)
    assert validation["valid"] is True
    assert validation["repositories"] == ["pricing", "catalog", "recovery"]
    assert len(validation["sha256"]) == 64

    submitted = cli(
        "task",
        "--fleet",
        str(fleet),
        "submit",
        "catalog",
        "--key",
        "cli-healthy",
    )
    assert submitted.returncode == 0, submitted.stderr
    task = json.loads(submitted.stdout)
    assert task["repository"] == "catalog"
    assert task["state"] == "queued"

    listed = cli("task", "--fleet", str(fleet), "list")
    assert listed.returncode == 0, listed.stderr
    assert [item["id"] for item in json.loads(listed.stdout)] == [task["id"]]

    worked = cli("worker", "--fleet", str(fleet), "--once")
    assert worked.returncode == 0, worked.stderr
    result = json.loads(worked.stdout)
    assert result["id"] == task["id"]
    assert result["state"] == "completed"
    assert result["result"]["outcome"] == "healthy"
    assert result["result"]["ai_calls"] == 0

    shown = cli("task", "--fleet", str(fleet), "show", task["id"])
    assert shown.returncode == 0, shown.stderr
    assert json.loads(shown.stdout)["state"] == "completed"


def test_cli_reports_invalid_flags_and_domain_errors(tmp_path):
    demo = tmp_path / "demo"
    initialized = cli("demo", "init", str(demo))
    assert initialized.returncode == 0, initialized.stderr
    fleet = Path(json.loads(initialized.stdout)["fleet"])

    invalid_flag = cli("worker", "--fleet", str(fleet), "--once", "--unknown")
    assert invalid_flag.returncode == 2
    assert "unrecognized arguments" in invalid_flag.stderr

    invalid_interval = cli("worker", "--fleet", str(fleet), "--once", "--interval", "0")
    assert invalid_interval.returncode == 2
    assert "interval must be positive" in invalid_interval.stderr

    unknown_repository = cli("task", "--fleet", str(fleet), "submit", "missing")
    assert unknown_repository.returncode == 2
    assert "Repository is not registered" in unknown_repository.stderr

    synthetic_demo = cli("demo", "run", str(demo))
    assert synthetic_demo.returncode == 2
    assert "requires --live-ai" in synthetic_demo.stderr

    nonempty = cli("demo", "init", str(demo))
    assert nonempty.returncode == 2
    assert "new or empty" in nonempty.stderr
