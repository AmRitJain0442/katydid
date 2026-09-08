"""Release authority regressions across real Git workspaces and controller state."""

import json
from pathlib import Path

import pytest
from test_controller import (
    DeterministicTestDouble,
    git,
    make_controller,
    make_repository,
    repository_policy,
)

import katydid.controller as controller_module
from katydid.fleet import DeliveryConfig

DEPLOY_CLEAN = """import sys
from pathlib import Path

release = Path(sys.argv[1])
release.mkdir(parents=True, exist_ok=True)
if Path("baseline-artifact.txt").exists():
    raise RuntimeError("release checkout inherited a check artifact")
(release / "deployed.txt").write_text("clean:" + sys.argv[2], encoding="utf-8")
"""

DEPLOY_MUTATES_TRACKED = """import sys
from pathlib import Path

Path("app.py").write_text("VALUE = 999\\n", encoding="utf-8")
release = Path(sys.argv[1])
release.mkdir(parents=True, exist_ok=True)
(release / "deployed.txt").write_text("mutated:" + sys.argv[2], encoding="utf-8")
"""

DEPLOY_WRITES_UNTRACKED = """import sys
from pathlib import Path

Path("unexpected.txt").write_text("unexpected", encoding="utf-8")
release = Path(sys.argv[1])
release.mkdir(parents=True, exist_ok=True)
(release / "deployed.txt").write_text("mutated:" + sys.argv[2], encoding="utf-8")
"""

DEPLOY_WRITES_IGNORED = """import sys
from pathlib import Path

Path("ignored.txt").write_text("ignored", encoding="utf-8")
release = Path(sys.argv[1])
release.mkdir(parents=True, exist_ok=True)
(release / "deployed.txt").write_text("mutated:" + sys.argv[2], encoding="utf-8")
"""

HEALTH_PASS = """import sys
from pathlib import Path

value = (Path(sys.argv[1]) / "deployed.txt").read_text(encoding="utf-8")
raise SystemExit(0 if value.startswith(("clean:", "mutated:")) else 1)
"""

ROLLBACK = """import sys
from pathlib import Path

target = Path(sys.argv[1])
target.mkdir(parents=True, exist_ok=True)
(target / "rollback.txt").write_text("rolled back", encoding="utf-8")
"""


def setup_release(
    tmp_path: Path,
    *,
    deploy: str = DEPLOY_CLEAN,
    untracked_check_artifact: bool = False,
):
    repository = make_repository(tmp_path, "service", release_scripts=True)
    if untracked_check_artifact:
        verifier = repository.joinpath("verify.py").read_text(encoding="utf-8")
        verifier = verifier.replace(
            'application = importlib.import_module("app")\n',
            'Path("baseline-artifact.txt").write_text("check-only", encoding="utf-8")\n'
            'application = importlib.import_module("app")\n',
        )
        repository.joinpath("verify.py").write_text(verifier, encoding="utf-8")
    repository.joinpath("deploy.py").write_text(deploy, encoding="utf-8")
    repository.joinpath("health.py").write_text(HEALTH_PASS, encoding="utf-8")
    repository.joinpath("rollback.py").write_text(ROLLBACK, encoding="utf-8")
    repository.joinpath(".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    git(
        repository,
        "add",
        "verify.py",
        "deploy.py",
        "health.py",
        "rollback.py",
        ".gitignore",
    )
    git(repository, "commit", "-m", "configure passing release fixture")
    provider = DeterministicTestDouble()
    controller = make_controller(tmp_path, [repository_policy(repository, release=True)], provider)
    task = controller.enqueue("service")
    return repository, provider, controller, task


def success_pointer(controller) -> Path:
    return controller.directory / "releases" / "service" / "last-success.json"


def transition_states(controller, task_id: str) -> list[str]:
    return [
        event["state"]
        for event in controller.store.events(task_id)
        if event["kind"] == "transition"
    ]


def advance_source(repository: Path, name: str) -> str:
    repository.joinpath(name).write_text("new base\n", encoding="utf-8")
    git(repository, "add", name)
    git(repository, "commit", "-m", f"advance source with {name}")
    return git(repository, "rev-parse", "HEAD")


def test_release_uses_fresh_checkout_without_untracked_check_artifacts(tmp_path: Path) -> None:
    repository, provider, controller, task = setup_release(tmp_path, untracked_check_artifact=True)

    result = controller.work_once()

    assert result["state"] == "completed", result
    assert result["result"]["outcome"] == "repaired"
    assert provider.roles == ["diagnosis", "repair", "review"]
    repair_workspace = Path(result["result"]["workspace"])
    release_workspace = Path(result["result"]["release_workspace"])
    assert repair_workspace != release_workspace
    assert (repair_workspace / "baseline-artifact.txt").read_text() == "check-only"
    assert not (release_workspace / "baseline-artifact.txt").exists()
    assert (
        git(release_workspace, "status", "--porcelain", "--untracked-files=all", "--ignored") == ""
    )
    pointer = json.loads(success_pointer(controller).read_text(encoding="utf-8"))
    assert pointer == {"commit": result["result"]["merged_sha"], "task": task["id"]}
    assert controller.store.events(task["id"])[-2]["kind"] == "release_success"
    assert (repository / "app.py").read_text(encoding="utf-8") == "VALUE = 2\n"


@pytest.mark.parametrize(
    ("deploy", "changed_path", "content"),
    [
        (DEPLOY_MUTATES_TRACKED, "app.py", "VALUE = 999\n"),
        (DEPLOY_WRITES_UNTRACKED, "unexpected.txt", "unexpected"),
        (DEPLOY_WRITES_IGNORED, "ignored.txt", "ignored"),
    ],
    ids=("tracked", "untracked", "ignored"),
)
def test_deploy_checkout_mutation_is_unresolved_before_health_or_success(
    tmp_path: Path, deploy: str, changed_path: str, content: str
) -> None:
    _repository, _provider, controller, task = setup_release(tmp_path, deploy=deploy)

    result = controller.work_once()

    assert result["state"] == "unresolved"
    assert "Release checkout differs from the verified commit" in result["result"]["error"]
    assert "monitoring" not in transition_states(controller, task["id"])
    assert result["result"].get("health") is None
    assert not success_pointer(controller).exists()
    assert all(event["kind"] != "release_success" for event in controller.store.events(task["id"]))
    release_workspace = Path(result["result"]["release_workspace"])
    assert (release_workspace / changed_path).read_text(encoding="utf-8") == content


def test_source_advance_immediately_after_real_merge_prevents_deploy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, _provider, controller, task = setup_release(tmp_path)
    real_merge = controller_module.merge_local
    advanced: list[str] = []

    def moving_merge(*args, **kwargs):
        merged = real_merge(*args, **kwargs)
        advanced.append(advance_source(repository, "after-merge.txt"))
        return merged

    monkeypatch.setattr(controller_module, "merge_local", moving_merge)

    result = controller.work_once()

    assert result["state"] == "unresolved"
    assert advanced
    assert result["result"]["merged_sha"] != advanced[0]
    assert "Release revision is no longer the current base" in result["result"]["error"]
    assert "deploying" not in transition_states(controller, task["id"])
    assert "release_workspace" not in result["result"]
    assert not (controller.directory / "releases" / "service" / "deployed.txt").exists()
    assert not success_pointer(controller).exists()


def test_source_advance_during_deploy_is_unresolved_before_health_or_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, _provider, controller, task = setup_release(tmp_path)
    real_run = controller_module.run_plan
    advanced: list[str] = []

    def moving_run(plan, *args, **kwargs):
        run = real_run(plan, *args, **kwargs)
        if plan.owner == "central-release-policy" and plan.checks[0].id == "deploy":
            advanced.append(advance_source(repository, "during-deploy.txt"))
        return run

    monkeypatch.setattr(controller_module, "run_plan", moving_run)

    result = controller.work_once()

    assert result["state"] == "unresolved"
    assert advanced
    assert "Release revision is no longer the current base" in result["result"]["error"]
    assert transition_states(controller, task["id"])[-1] == "unresolved"
    assert result["result"].get("health") is None
    assert not success_pointer(controller).exists()
    assert all(event["kind"] != "release_success" for event in controller.store.events(task["id"]))


def test_pause_after_passing_health_cannot_record_release_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _repository, _provider, controller, task = setup_release(tmp_path)
    real_run = controller_module.run_plan
    pauses: list[dict] = []

    def pausing_run(plan, *args, **kwargs):
        run = real_run(plan, *args, **kwargs)
        if plan.owner == "central-release-policy" and plan.checks[0].id == "health":
            pauses.append(controller.store.control(task["id"], "pause"))
        return run

    monkeypatch.setattr(controller_module, "run_plan", pausing_run)

    result = controller.work_once()

    assert pauses and pauses[0]["state"] == "unresolved"
    assert result["state"] == "unresolved"
    outcome = json.loads(
        (controller.directory / "tasks" / task["id"] / "1" / "outcome.json").read_text(
            encoding="utf-8"
        )
    )
    assert outcome["release_hooks"][-1]["gate"]["passed"] is True
    assert not success_pointer(controller).exists()
    assert all(event["kind"] != "release_success" for event in controller.store.events(task["id"]))


def test_github_release_waits_for_exact_commit_checks_before_deploy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = make_repository(tmp_path, "service", release_scripts=True)
    repository.joinpath("app.py").write_text("VALUE = 2\n", encoding="utf-8")
    repository.joinpath("deploy.py").write_text(DEPLOY_CLEAN, encoding="utf-8")
    repository.joinpath("health.py").write_text(HEALTH_PASS, encoding="utf-8")
    repository.joinpath("rollback.py").write_text(ROLLBACK, encoding="utf-8")
    git(repository, "add", "app.py", "deploy.py", "health.py", "rollback.py")
    git(repository, "commit", "-m", "healthy release fixture")
    provider = DeterministicTestDouble()
    controller = make_controller(
        tmp_path,
        [repository_policy(repository, release=True)],
        provider,
    )
    local_repo = controller.config.repositories[0]
    github_delivery = DeliveryConfig(
        mode="github",
        github_repository="owner/service",
        auto_merge=True,
        github_required_checks=["CI / required"],
    )
    github_repo = local_repo.model_copy(update={"delivery": github_delivery})
    controller.config = controller.config.model_copy(update={"repositories": [github_repo]})
    task = controller.enqueue("service", stage="release", mode="release")
    expected_sha = git(repository, "rev-parse", "HEAD")
    deployed = controller.directory / "releases" / "service" / "deployed.txt"
    calls = []

    def passing_checks(repo, sha, cancelled, required_checks, timeout_seconds=900):
        assert not deployed.exists()
        calls.append((repo, sha, cancelled, required_checks, timeout_seconds))
        return {
            "repository": repo,
            "sha": sha,
            "checks": [{"name": "CI / required", "kind": "check_run", "state": "success"}],
        }

    monkeypatch.setattr(controller_module, "wait_commit_checks", passing_checks)

    result = controller.work_once()

    assert result["state"] == "completed", result
    assert result["result"]["outcome"] == "released"
    assert calls and calls[0][0:2] == ("owner/service", expected_sha)
    assert calls[0][3] == ("CI / required",)
    assert result["result"]["github_release_checks"]["sha"] == expected_sha
    assert deployed.read_text(encoding="utf-8") == f"clean:{expected_sha}"
    assert success_pointer(controller).exists()
    states = transition_states(controller, task["id"])
    assert states.index("verifying") < states.index("deploying")
