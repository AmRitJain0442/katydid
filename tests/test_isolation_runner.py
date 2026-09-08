import json
import signal
import subprocess

import pytest
import yaml
from test_controller import (
    DeterministicTestDouble,
    git,
    make_controller,
    make_repository,
    repository_policy,
)

from katydid import runner
from katydid.cli import main
from katydid.evidence import CheckResult, Status, read_junit
from katydid.profile import isolation_file, make_plan

IMAGE = "python@sha256:" + "a" * 64


@pytest.mark.parametrize(
    "filename", [".git./config", ".git /config", "app.py:secret", "NUL", "a\nb.py"]
)
def test_portable_source_paths_reject_windows_aliases_and_streams(filename):
    with pytest.raises(ValueError):
        isolation_file(filename)


class DockerBoundaryDouble:
    mode = "healthy"
    calls = []

    def __init__(self, plan, directory, run_id, write_json):
        self.ready = False
        self.errors = []

    def prepare(self):
        if self.mode == "unavailable":
            self.errors.append("Docker daemon unavailable")
            raise RuntimeError(self.errors[-1])
        self.ready = True

    def execute(self, check, folder, cancel, cancel_file, environment_directory):
        self.calls.append(check.id)
        report = folder / "boundary-report.xml"
        report.write_text('<testsuite><testcase name="contract"/></testsuite>')
        if self.mode == "cleanup-failure":
            self.errors.append("Container removal could not be verified")
        return CheckResult(
            check.id,
            Status.PASSED,
            "Passed inside test boundary",
            0,
            0.1,
            read_junit(report) if check.kind == "test" else None,
        )

    def snapshot(self):
        return {
            "ready": self.ready,
            "cleanup_complete": not self.errors,
            "errors": list(self.errors),
            "resources": [],
        }

    def cleanup(self):
        pass


@pytest.fixture
def boundary(monkeypatch):
    from katydid import docker

    DockerBoundaryDouble.mode = "healthy"
    DockerBoundaryDouble.calls = []
    monkeypatch.setattr(docker, "DockerSession", DockerBoundaryDouble)

    def forbidden_local(*args, **kwargs):
        raise AssertionError("An isolated command reached the host executor")

    monkeypatch.setattr(runner, "_execute", forbidden_local)
    return DockerBoundaryDouble


def add_isolation(repository):
    path = repository / "quality.yaml"
    profile = yaml.safe_load(path.read_text())
    profile["isolation"] = {"adapter": "docker", "image": IMAGE, "files": ["app.py", "verify.py"]}
    path.write_text(yaml.safe_dump(profile))
    return path


@pytest.mark.parametrize("mode", ["unavailable", "cleanup-failure"])
def test_isolation_failure_never_falls_back_or_invokes_ai(tmp_path, boundary, mode):
    boundary.mode = mode
    repository = make_repository(tmp_path, "service")
    add_isolation(repository)
    git(repository, "add", "quality.yaml")
    git(repository, "commit", "-m", "require container execution")
    head = git(repository, "rev-parse", "HEAD")
    provider = DeterministicTestDouble()
    controller = make_controller(tmp_path, [repository_policy(repository)], provider)
    controller.enqueue("service", "docker-failure")

    result = controller.work_once()

    assert result["state"] == "failed"
    assert provider.roles == []
    assert git(repository, "rev-parse", "HEAD") == head
    summaries = list((tmp_path / "control-state").rglob("run.json"))
    assert summaries
    summary = json.loads(summaries[0].read_text())
    assert not summary["gate"]["passed"]
    assert summary["isolation"]["errors"]
    if mode == "cleanup-failure":
        assert summary["results"][0]["status"] == "passed"


def test_all_lifecycle_hooks_use_the_selected_executor(tmp_path, boundary):
    repository = make_repository(tmp_path, "service")
    path = add_isolation(repository)
    profile = yaml.safe_load(path.read_text())

    def hook(name):
        return {"id": name, "kind": "command", "argv": ["{python}", "verify.py"]}

    profile["environment"] = {
        "prepare": [hook("prepare")],
        "readiness": {"check": hook("ready")},
        "cleanup": [hook("cleanup")],
    }
    path.write_text(yaml.safe_dump(profile))
    run = runner.run_plan(make_plan(path, "pull-request"))
    assert run.gate.passed
    assert boundary.calls == ["prepare", "ready", "unit", "cleanup"]
    assert run.environment.cleanup_complete
    assert run.isolation["ready"]


def test_sweeper_cli_reports_daemon_outage_and_rejects_invalid_scope(monkeypatch, capsys):
    from katydid import docker

    def unavailable(namespace):
        raise RuntimeError("daemon unavailable")

    monkeypatch.setattr(docker, "sweep", unavailable)
    assert main(["sweep", "--namespace", "test-scope"]) == 1
    assert json.loads(capsys.readouterr().out)["errors"] == ["daemon unavailable"]
    assert main(["sweep", "--namespace", "*"]) == 2


def test_watch_sweeper_survives_daemon_timeout_until_interrupted(monkeypatch, capsys):
    from katydid import docker

    calls = []

    def intermittent(namespace):
        calls.append(namespace)
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(["docker", "ps"], 30)
        signal.raise_signal(signal.SIGTERM)
        return {"removed": [], "skipped": [], "errors": []}

    monkeypatch.setattr(docker, "sweep", intermittent)
    assert main(["sweep", "--namespace", "watch-scope", "--watch", "--interval", "1"]) == 0
    reports = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(calls) == len(reports) == 2
    assert reports[0]["errors"]
    assert not reports[1]["errors"]
