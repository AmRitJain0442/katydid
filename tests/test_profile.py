import json
from pathlib import Path

import pytest

from katydid.cli import main
from katydid.profile import ProfileError, make_plan

BASE = """schema_version: 1
repository: demo
owner: team
checks:
  - id: unit
    kind: test
    argv: [python, test.py]
"""


def profile(tmp_path: Path, text: str = BASE) -> Path:
    path = tmp_path / "quality.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_plan_is_deterministic_and_does_not_execute(tmp_path):
    path = profile(tmp_path)
    first = make_plan(path, "pull-request")
    assert first == make_plan(path, "pull-request")
    assert first.checks[0].argv == ("python", "test.py")
    assert len(first.profile_sha256) == 64
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize(
    "text",
    [
        BASE + "unknown: true\n",
        BASE.replace("schema_version: 1", "schema_version: true"),
        BASE.replace("schema_version: 1", "schema_version: 2"),
        BASE.replace("kind: test", "kind: mystery"),
        BASE.replace("argv: [python, test.py]", "argv: python test.py"),
        BASE.replace("argv: [python, test.py]", "argv: []"),
        BASE.replace("argv: [python, test.py]", "argv: [3]"),
        BASE.replace("kind: test", "kind: test\n    required: 'false'"),
        BASE.replace("kind: test", "kind: test\n    timeout_seconds: 0"),
        BASE.replace("kind: test", "kind: test\n    timeout_seconds: true"),
        BASE.replace("kind: test", "kind: test\n    stages: [nightly, nightly]"),
        BASE.replace("owner: team", "owner: '  '"),
        BASE + "owner: duplicate\n",
        BASE.replace("[python, test.py]", "&cmd [python, test.py]"),
        "!!python/object/apply:os.system ['echo should-not-execute']",
        "checks: []",
        "",
    ],
)
def test_rejects_ambiguous_or_invalid_profiles(tmp_path, text):
    with pytest.raises(ProfileError):
        make_plan(profile(tmp_path, text), "pull-request")


@pytest.mark.parametrize("directory", ["..", "../outside", "/tmp", "C:/Windows", "C:relative"])
def test_working_directory_cannot_escape_root(tmp_path, directory):
    text = BASE.replace("kind: test", f"kind: test\n    working_directory: '{directory}'")
    with pytest.raises(ProfileError):
        make_plan(profile(tmp_path, text), "pull-request")


def test_symlink_cannot_escape_root(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    try:
        (root / "escape").symlink_to(tmp_path, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symlinks requires privileges on this Windows runner")
    with pytest.raises(ProfileError):
        make_plan(
            profile(root, BASE.replace("kind: test", "kind: test\n    working_directory: escape")),
            "pull-request",
        )


def test_duplicate_check_ids_are_rejected(tmp_path):
    with pytest.raises(ProfileError, match="unique"):
        make_plan(
            profile(tmp_path, BASE + "  - id: unit\n    kind: command\n    argv: [echo]\n"),
            "pull-request",
        )


def test_empty_or_advisory_only_selection_cannot_pass(tmp_path):
    with pytest.raises(ProfileError, match="required"):
        make_plan(profile(tmp_path), "nightly")
    with pytest.raises(ProfileError, match="required"):
        make_plan(
            profile(tmp_path, BASE.replace("kind: test", "kind: test\n    required: false")),
            "pull-request",
        )


def test_stage_exclusions_are_explicit(tmp_path):
    text = BASE + "  - id: deep\n    kind: command\n    argv: [echo]\n    stages: [nightly]\n"
    plan = make_plan(profile(tmp_path, text), "pull-request")
    assert [check.id for check in plan.checks] == ["unit"]
    assert plan.excluded == ("deep: not configured for stage pull-request",)


def test_cli_reports_errors_and_machine_readable_plan(tmp_path, capsys):
    path = profile(tmp_path)
    assert main(["plan", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["repository"] == "demo"
    assert main(["validate", str(tmp_path / "missing.yaml")]) == 2
    assert "Cannot load profile" in capsys.readouterr().err


def test_profile_digest_changes_with_source(tmp_path):
    path = profile(tmp_path)
    before = make_plan(path, "pull-request").profile_sha256
    path.write_text(BASE + "# new revision\n", encoding="utf-8")
    assert make_plan(path, "pull-request").profile_sha256 != before
