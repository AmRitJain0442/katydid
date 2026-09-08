from pathlib import Path

import pytest
import yaml

from katydid.fleet import enforce_policy, load_fleet
from katydid.profile import ProfileError, make_plan

IMAGE = "registry.example/katydid/python@sha256:" + "a" * 64
OTHER_IMAGE = "registry.example/katydid/python@sha256:" + "b" * 64


def isolation(**changes):
    value = {
        "adapter": "docker",
        "image": IMAGE,
        "files": ["app.py", "test.py"],
    }
    value.update(changes)
    return value


def profile_data(isolation_value=...):
    value = {
        "schema_version": 1,
        "repository": "demo",
        "owner": "team",
        "checks": [
            {
                "id": "unit",
                "kind": "test",
                "argv": ["python", "test.py"],
            }
        ],
    }
    if isolation_value is not ...:
        value["isolation"] = isolation_value
    return value


def write_profile(tmp_path: Path, value: dict) -> Path:
    root = tmp_path / "repo"
    root.mkdir(exist_ok=True)
    (root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "test.py").write_text(
        "from pathlib import Path; Path('executed').touch()\n", encoding="utf-8"
    )
    path = root / "quality.yaml"
    path.write_text(yaml.safe_dump(value), encoding="utf-8")
    return path


def plan(tmp_path: Path, isolation_value=...):
    path = write_profile(tmp_path, profile_data(isolation_value))
    return make_plan(path, "pull-request")


def fleet_data(isolation_policy=...):
    repository = {
        "id": "demo",
        "source": "repo",
        "context_paths": ["app.py", "test.py"],
        "editable_paths": ["app.py"],
        "requirements": "Preserve the tested contract.",
        "required_checks": {"unit": "test"},
    }
    if isolation_policy is not ...:
        repository["isolation_policy"] = isolation_policy
    return {"schema_version": 1, "repositories": [repository]}


def policy(**changes):
    value = {
        "required": True,
        "images": [IMAGE],
        "files": ["app.py", "test.py"],
        "namespace": "katydid",
        "max_cpus": 1.0,
        "max_memory_mb": 512,
        "max_pids": 128,
        "max_tmpfs_mb": 64,
    }
    value.update(changes)
    return value


def load_policy(tmp_path: Path, value: dict):
    path = tmp_path / "fleet.yaml"
    path.write_text(yaml.safe_dump(value), encoding="utf-8")
    return load_fleet(path)[0].repository("demo")


def test_legacy_profile_and_fleet_remain_local_and_compatible(tmp_path):
    legacy = plan(tmp_path)
    repository = load_policy(tmp_path, fleet_data())

    assert legacy.isolation is None
    enforce_policy(repository, legacy)
    assert not (tmp_path / "repo" / "executed").exists()


def test_isolation_plan_has_strict_defaults_without_running_docker_or_code(tmp_path):
    isolated = plan(tmp_path, isolation())

    assert isolated.isolation is not None
    assert isolated.isolation.adapter == "docker"
    assert isolated.isolation.image == IMAGE
    assert isolated.isolation.files == ("app.py", "test.py")
    assert isolated.isolation.namespace == "katydid"
    assert isolated.isolation.cpus == 1.0
    assert isolated.isolation.memory_mb == 512
    assert isolated.isolation.pids_limit == 128
    assert isolated.isolation.tmpfs_mb == 64
    assert not (tmp_path / "repo" / "executed").exists()


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(adapter="local"),
        lambda value: value.update(image="python:3.12"),
        lambda value: value.update(image="python@sha256:" + "a" * 63),
        lambda value: value.update(files=[]),
        lambda value: value.update(files=["app.py", "app.py"]),
        lambda value: value.update(files=["app.py", "APP.py"]),
        lambda value: value.update(files=[f"file-{index}.py" for index in range(501)]),
        lambda value: value.update(files=["../outside.py"]),
        lambda value: value.update(files=["/etc/passwd"]),
        lambda value: value.update(files=["C:/Windows/win.ini"]),
        lambda value: value.update(files=[".git/config"]),
        lambda value: value.update(files=[".env"]),
        lambda value: value.update(namespace="Team_A"),
        lambda value: value.update(cpus=0.09),
        lambda value: value.update(cpus=8.01),
        lambda value: value.update(cpus=True),
        lambda value: value.update(memory_mb=63),
        lambda value: value.update(memory_mb=8193),
        lambda value: value.update(pids_limit=15),
        lambda value: value.update(pids_limit=1025),
        lambda value: value.update(tmpfs_mb=15),
        lambda value: value.update(tmpfs_mb=1025),
        lambda value: value.update(network="bridge"),
        lambda value: value.update(environment={"TOKEN": "secret"}),
        lambda value: value.update(mounts=["/var/run/docker.sock"]),
        lambda value: value.update(docker_flags=["--privileged"]),
    ],
)
def test_isolation_rejects_unsafe_or_unbounded_profile_fields(tmp_path, mutation):
    value = isolation()
    mutation(value)
    with pytest.raises(ProfileError):
        plan(tmp_path, value)


@pytest.mark.parametrize(
    "filename",
    [
        ".ssh/id_rsa",
        ".aws/credentials",
        ".azure/accessTokens.json",
        ".docker/config.json",
        ".codex/auth.json",
        ".netrc",
        ".npmrc",
        "config/.env.production",
        "certificates/client.pem",
        "certificates/client.key",
        "certificates/client.p12",
        "certificates/client.pfx",
    ],
)
def test_isolation_rejects_common_credential_paths(tmp_path, filename):
    with pytest.raises(ProfileError):
        plan(tmp_path, isolation(files=[filename]))


@pytest.mark.parametrize("files", [["missing.py"], ["snapshot"]])
def test_isolation_files_must_be_existing_regular_files(tmp_path, files):
    path = write_profile(tmp_path, profile_data(isolation(files=files)))
    (path.parent / "snapshot").mkdir()

    with pytest.raises(ProfileError):
        make_plan(path, "pull-request")


def test_isolation_rejects_a_link_even_when_it_points_inside_source(tmp_path):
    path = write_profile(tmp_path, profile_data(isolation(files=["linked.py"])))
    try:
        (path.parent / "linked.py").symlink_to(path.parent / "app.py")
    except OSError:
        pytest.skip("Creating symlinks requires privileges on this runner")

    with pytest.raises(ProfileError):
        make_plan(path, "pull-request")


def test_check_working_directory_must_be_represented_in_snapshot(tmp_path):
    value = profile_data(isolation())
    value["checks"][0]["working_directory"] = "other"
    path = write_profile(tmp_path, value)
    (path.parent / "other").mkdir()

    with pytest.raises(ProfileError):
        make_plan(path, "pull-request")


def test_environment_hook_working_directory_must_be_represented_in_snapshot(tmp_path):
    value = profile_data(isolation())
    value["environment"] = {
        "prepare": [
            {
                "id": "prepare",
                "kind": "command",
                "argv": ["python", "prepare.py"],
                "working_directory": "other",
            }
        ],
        "cleanup": [{"id": "cleanup", "kind": "command", "argv": ["python", "cleanup.py"]}],
    }
    path = write_profile(tmp_path, value)
    (path.parent / "other").mkdir()

    with pytest.raises(ProfileError):
        make_plan(path, "pull-request")


def test_profile_may_request_docker_without_claiming_central_requirement(tmp_path):
    isolated = plan(tmp_path, isolation())
    repository = load_policy(tmp_path, fleet_data())

    enforce_policy(repository, isolated)


def test_optional_central_policy_allows_legacy_local_profile(tmp_path):
    legacy = plan(tmp_path)
    repository = load_policy(tmp_path, fleet_data(policy(required=False)))

    enforce_policy(repository, legacy)


def test_required_central_policy_rejects_isolation_downgrade(tmp_path):
    legacy = plan(tmp_path)
    repository = load_policy(tmp_path, fleet_data(policy()))

    with pytest.raises(ProfileError):
        enforce_policy(repository, legacy)


@pytest.mark.parametrize(
    "profile_changes",
    [
        {"image": OTHER_IMAGE},
        {"files": ["app.py", "test.py", "extra.py"]},
        {"namespace": "another"},
        {"cpus": 1.1},
        {"memory_mb": 513},
        {"pids_limit": 129},
        {"tmpfs_mb": 65},
    ],
)
def test_central_policy_rejects_unapproved_or_excess_isolation(tmp_path, profile_changes):
    value = isolation(**profile_changes)
    path = write_profile(tmp_path, profile_data(value))
    (path.parent / "extra.py").write_text("EXTRA = True\n", encoding="utf-8")
    isolated = make_plan(path, "pull-request")
    repository = load_policy(tmp_path, fleet_data(policy()))

    with pytest.raises(ProfileError):
        enforce_policy(repository, isolated)


def test_central_policy_accepts_approved_subset_and_resource_limits(tmp_path):
    isolated = plan(
        tmp_path,
        isolation(cpus=0.5, memory_mb=256, pids_limit=64, tmpfs_mb=32),
    )
    repository = load_policy(
        tmp_path,
        fleet_data(policy(files=["app.py", "test.py", "optional.py"])),
    )

    enforce_policy(repository, isolated)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(required="true"),
        lambda value: value.update(images=[]),
        lambda value: value.update(images=["python:3.12"]),
        lambda value: value.update(files=[]),
        lambda value: value.update(files=["app.py", "app.py"]),
        lambda value: value.update(files=["app.py", "APP.py"]),
        lambda value: value.update(files=[".env"]),
        lambda value: value.update(files=[".ssh/id_rsa"]),
        lambda value: value.update(files=["private.key"]),
        lambda value: value.update(namespace="Team_A"),
        lambda value: value.update(max_cpus=8.1),
        lambda value: value.update(max_memory_mb=8193),
        lambda value: value.update(max_pids=1025),
        lambda value: value.update(max_tmpfs_mb=1025),
        lambda value: value.update(network="none"),
    ],
)
def test_fleet_rejects_ambiguous_or_unsafe_isolation_policy(tmp_path, mutation):
    value = policy()
    mutation(value)

    with pytest.raises(ProfileError):
        load_policy(tmp_path, fleet_data(value))
