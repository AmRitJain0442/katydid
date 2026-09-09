import copy
from dataclasses import replace

import pytest
import yaml

from katydid.fleet import enforce_policy, load_fleet
from katydid.profile import Plan, PlannedCheck, ProfileError


def data():
    return {
        "schema_version": 1,
        "repositories": [
            {
                "id": "demo",
                "source": "repo",
                "context_paths": ["app.py", "test_app.py"],
                "editable_paths": ["app.py"],
                "requirements": "The application must preserve its invariants.",
                "required_checks": {"unit": "test"},
            }
        ],
    }


def load(tmp_path, value):
    path = tmp_path / "fleet.yaml"
    path.write_text(yaml.safe_dump(value), encoding="utf-8")
    return load_fleet(path)


def test_fleet_resolves_paths_and_digests(tmp_path):
    config, digest = load(tmp_path, data())
    assert config.repository("demo").source == str(tmp_path / "repo")
    assert config.state_directory == str(tmp_path / ".katydid" / "control")
    assert len(digest) == 64


@pytest.mark.parametrize(
    "mutation",
    [
        lambda d: d.update(schema_version=True),
        lambda d: d.update(unknown="field"),
        lambda d: d["repositories"][0].update(profile="../quality.yaml"),
        lambda d: d["repositories"][0].update(context_paths=[".env"]),
        lambda d: d["repositories"][0].update(
            editable_paths=["quality.yaml"], context_paths=["quality.yaml"]
        ),
        lambda d: d["repositories"][0].update(editable_paths=["unlisted.py"]),
        lambda d: d["repositories"][0].update(required_checks={}),
        lambda d: d["repositories"][0].update(source="ssh://somewhere/repo"),
        lambda d: d["repositories"][0].update(github_comments=True),
        lambda d: d["repositories"][0].update(github_comments="true"),
        lambda d: d["repositories"][0].update(delivery={"mode": "github"}),
        lambda d: d["repositories"][0].update(delivery={"auto_merge": True}),
        lambda d: d["repositories"].append(dict(d["repositories"][0])),
    ],
)
def test_fleet_rejects_ambiguous_or_unsafe_policy(tmp_path, mutation):
    value = data()
    mutation(value)
    with pytest.raises(ProfileError):
        load(tmp_path, value)


def test_github_comments_require_explicit_registered_repository_opt_in(tmp_path):
    value = data()
    value["repositories"][0]["source"] = "https://github.com/owner/repository.git"
    config, _ = load(tmp_path, value)
    assert config.repository("demo").github_comments is False
    value["repositories"][0]["github_comments"] = True
    config, _ = load(tmp_path, value)
    assert config.repository("demo").github_comments is True


def test_central_checks_cannot_be_weakened(tmp_path):
    config, _ = load(tmp_path, data())
    repo = config.repository("demo")
    check = PlannedCheck("unit", "test", ("python",), ".", 30, True)
    plan = Plan(1, "demo", "team", "pull-request", ".", "quality.yaml", "hash", (check,), ())
    enforce_policy(repo, plan)
    for checks in ((), (replace(check, required=False),), (replace(check, kind="command"),)):
        with pytest.raises(ProfileError):
            enforce_policy(repo, replace(plan, checks=checks))


def test_unregistered_repository_is_rejected(tmp_path):
    config, _ = load(tmp_path, data())
    with pytest.raises(ProfileError):
        config.repository("unregistered")


def test_same_normalized_git_source_cannot_be_registered_under_multiple_ids(tmp_path):
    value = data()
    duplicate = copy.deepcopy(value["repositories"][0])
    duplicate.update(id="another", source="./repo")
    value["repositories"].append(duplicate)

    with pytest.raises(ProfileError, match="source can only be registered once"):
        load(tmp_path, value)


def test_release_auto_deploy_is_strict_and_defaults_off(tmp_path):
    value = data()
    value["repositories"][0]["delivery"] = {"mode": "local", "auto_merge": True}
    value["repositories"][0]["release"] = {
        "deploy": {"id": "deploy", "kind": "command", "argv": ["python", "hook.py"]},
        "health": {"id": "health", "kind": "command", "argv": ["python", "hook.py"]},
        "rollback": {
            "id": "rollback",
            "kind": "command",
            "argv": ["python", "hook.py"],
        },
    }

    config, _ = load(tmp_path, value)
    assert config.repositories[0].release.auto_deploy is False
    value["repositories"][0]["release"]["auto_deploy"] = True
    config, _ = load(tmp_path, value)
    assert config.repositories[0].release.auto_deploy is True
    value["repositories"][0]["release"]["auto_deploy"] = "true"
    with pytest.raises(ProfileError):
        load(tmp_path, value)
