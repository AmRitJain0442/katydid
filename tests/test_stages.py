import json
from pathlib import Path

import pytest
import yaml
from test_controller import (
    DeterministicTestDouble,
    git,
    make_controller,
    make_repository,
    repository_policy,
)

from katydid import controller as controller_module
from katydid.fleet import enforce_policy
from katydid.profile import ProfileError, make_plan
from katydid.tasks import TaskRequest
from katydid.workspace import prepare_workspace, source_head, source_revision

STAGES = ("pull-request", "merge", "nightly", "release")


def staged_repository(root, name, *, healthy=True, release=False):
    repository = make_repository(root, name, release_scripts=release)
    if healthy:
        (repository / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    profile_path = repository / "quality.yaml"
    profile = yaml.safe_load(profile_path.read_text())
    profile["checks"][0]["stages"] = list(STAGES)
    for stage in STAGES:
        profile["checks"].append(
            {
                "id": stage,
                "kind": "command",
                "argv": ["{python}", "-c", f"print({stage!r})"],
                "stages": [stage],
            }
        )
    profile_path.write_text(yaml.safe_dump(profile), encoding="utf-8")
    git(repository, "add", ".")
    git(repository, "commit", "-m", "configure all four verification stages")
    return repository


@pytest.mark.parametrize("stage", STAGES)
def test_two_repositories_run_only_requested_stage_without_ai(tmp_path, stage):
    repositories = [staged_repository(tmp_path, name) for name in ("service", "library")]
    provider = DeterministicTestDouble()
    policies = [repository_policy(repo) for repo in repositories]
    for policy in policies:
        policy["required_checks_by_stage"] = {lane: {lane: "command"} for lane in STAGES}
    controller = make_controller(tmp_path, policies, provider)
    for repo in repositories:
        task = controller.enqueue(repo.name, stage=stage, mode="check")
        result = controller.work_once()
        assert result["id"] == task["id"]
        assert result["state"] == "completed", result
        evidence = result["result"]
        assert evidence["stage"] == stage and evidence["mode"] == "check"
        assert evidence["source_sha"] == source_head(str(repo), "main")
        assert [item["id"] for item in evidence["baseline"]["results"]] == ["unit", stage]
        assert evidence["ai_calls"] == 0
        run = json.loads((Path(evidence["baseline"]["directory"]) / "run.json").read_text())
        assert run["plan"]["stage"] == stage
        assert run["git"]["head"] == evidence["source_sha"]
    assert provider.roles == []


def test_merge_and_nightly_discovery_are_independent_and_idempotent(tmp_path):
    repository = staged_repository(tmp_path, "service")
    provider = DeterministicTestDouble()
    controller = make_controller(tmp_path, [repository_policy(repository)], provider)
    merged = controller.discover()[0]
    nightly = controller.discover(period=7)[0]
    assert merged["id"] != nightly["id"]
    assert merged["payload"]["stage"] == "merge"
    assert nightly["payload"]["stage"] == "nightly"
    assert controller.discover()[0]["id"] == merged["id"]
    assert controller.discover(period=7)[0]["id"] == nightly["id"]
    assert controller.discover(period=8)[0]["id"] != nightly["id"]
    for _ in range(3):
        assert controller.work_once()["state"] == "completed"
    assert provider.roles == []


def test_validation_failure_does_not_invoke_ai_or_publish(tmp_path):
    repository = staged_repository(tmp_path, "service", healthy=False)
    provider = DeterministicTestDouble()
    controller = make_controller(tmp_path, [repository_policy(repository)], provider)
    before = source_head(str(repository), "main")
    controller.enqueue("service", stage="nightly", mode="check")
    result = controller.work_once()
    assert result["state"] == "failed"
    assert "does not permit AI repair" in result["result"]["error"]
    assert not result["result"]["baseline"]["gate"]["passed"]
    assert source_head(str(repository), "main") == before
    assert provider.roles == []


def test_central_stage_check_cannot_be_omitted(tmp_path):
    repository = staged_repository(tmp_path, "service")
    provider = DeterministicTestDouble()
    policy = repository_policy(repository)
    policy["required_checks_by_stage"] = {"nightly": {"missing-security": "command"}}
    controller = make_controller(tmp_path, [policy], provider)
    with pytest.raises(ProfileError, match="missing-security"):
        enforce_policy(
            controller.config.repository("service"),
            make_plan(repository / "quality.yaml", "nightly"),
        )
    controller.enqueue("service", stage="nightly", mode="check")
    result = controller.work_once()
    assert result["state"] == "failed"
    assert "baseline" not in result["result"]


def test_source_changed_during_checks_cannot_complete_as_healthy(tmp_path, monkeypatch):
    repository = staged_repository(tmp_path, "service")
    controller = make_controller(
        tmp_path, [repository_policy(repository)], DeterministicTestDouble()
    )
    execute = controller_module.run_plan

    def moving_source(*args, **kwargs):
        run = execute(*args, **kwargs)
        (repository / "new.txt").write_text("new revision")
        git(repository, "add", "new.txt")
        git(repository, "commit", "-m", "supersede tested revision")
        return run

    monkeypatch.setattr(controller_module, "run_plan", moving_source)
    controller.enqueue("service", stage="merge", mode="check")
    result = controller.work_once()
    assert result["state"] == "failed"
    assert result["result"]["baseline"]["gate"]["passed"]
    assert "Source head changed" in result["result"]["error"]


@pytest.mark.parametrize("healthy", [True, False])
def test_release_stage_checks_gate_actual_deployment(tmp_path, healthy):
    repository = staged_repository(tmp_path, "service", healthy=healthy, release=True)
    policy = repository_policy(repository, release=True)
    policy["release"]["health"]["argv"] = [
        "{python}",
        "-c",
        "import pathlib,sys; "
        "assert pathlib.Path(sys.argv[1], 'deployed.txt').read_text() == 'new:' + sys.argv[2]",
        "{release_dir}",
        "{commit}",
    ]
    provider = DeterministicTestDouble()
    controller = make_controller(tmp_path, [policy], provider)
    controller.enqueue("service", stage="release", mode="release")
    result = controller.work_once()
    target = controller.directory / "releases" / "service" / "deployed.txt"
    assert provider.roles == []
    if healthy:
        assert result["state"] == "completed", result
        assert result["result"]["outcome"] == "released"
        assert target.read_text() == "new:" + source_head(str(repository), "main")
        assert result["result"]["health"]["gate"]["passed"]
    else:
        assert result["state"] == "failed"
        assert not target.exists()


def test_exact_pr_ref_checkout_and_annotated_tag_resolution(tmp_path):
    repository = staged_repository(tmp_path, "service")
    base = source_head(str(repository), "main")
    git(repository, "switch", "-c", "feature")
    (repository / "app.py").write_text("VALUE = 3\n")
    git(repository, "commit", "-am", "candidate change")
    head = git(repository, "rev-parse", "HEAD")
    git(repository, "update-ref", "refs/pull/7/head", head)
    git(repository, "tag", "-a", "v1", "-m", "annotated candidate")
    git(repository, "switch", "main")
    assert source_revision(str(repository), "refs/pull/7/head") == head
    assert source_revision(str(repository), "refs/tags/v1") == head
    workspace = prepare_workspace(
        str(repository),
        tmp_path / "workspace",
        "main",
        "katydid/check",
        source_ref="refs/pull/7/head",
        source_sha=head,
    )
    assert workspace.base_sha == base
    assert git(workspace.path, "rev-parse", "HEAD") == head
    assert (workspace.path / "app.py").read_text() == "VALUE = 3\n"


def test_pr_source_requires_isolation_and_cannot_request_repair(tmp_path):
    repository = staged_repository(tmp_path, "service")
    controller = make_controller(
        tmp_path, [repository_policy(repository)], DeterministicTestDouble()
    )
    sha = source_head(str(repository), "main")
    payload = dict(
        config_sha256=controller.digest,
        base_sha=sha,
        source_sha=sha,
        source_ref="refs/pull/7/head",
        stage="pull-request",
        mode="check",
    )
    controller.store.create_task("service", payload)
    result = controller.work_once()
    assert result["state"] == "failed"
    assert "centrally enforced Docker" in result["result"]["error"]
    with pytest.raises(ValueError, match="only pull-request checks"):
        TaskRequest.model_validate({**payload, "mode": "repair"})


@pytest.mark.parametrize(
    "change",
    [
        {"required_checks_by_stage": {"nightly": {"unit": "command"}}},
        {"required_checks_by_stage": {"unknown-stage": {"unit": "test"}}},
        {"events": {"github_repository": "firm/service", "releases": True}},
    ],
)
def test_invalid_stage_or_event_policy_is_rejected(tmp_path, change):
    repository = staged_repository(tmp_path, "service")
    policy = {**repository_policy(repository), **change}
    with pytest.raises(ProfileError):
        make_controller(tmp_path, [policy], DeterministicTestDouble())


def test_duplicate_event_identity_is_rejected(tmp_path):
    repositories = [staged_repository(tmp_path, name) for name in ("a", "b")]
    policies = [repository_policy(repo) for repo in repositories]
    for policy in policies:
        policy["events"] = {"github_repository": "firm/service"}
    with pytest.raises(ProfileError, match="identities must be unique"):
        make_controller(tmp_path, policies, DeterministicTestDouble())
