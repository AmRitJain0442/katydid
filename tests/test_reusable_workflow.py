import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "reusable-checks.yml"


def embedded_guards() -> tuple[str, str]:
    source = WORKFLOW.read_text(encoding="utf-8")
    matches = re.findall(
        r"run: \|\n          python3 - <<'PY'\n(.*?)\n          PY",
        source,
        flags=re.DOTALL,
    )
    assert len(matches) == 2
    return tuple(textwrap.dedent(match) for match in matches)  # type: ignore[return-value]


def validation_environment() -> dict[str, str]:
    return {
        **os.environ,
        "KATYDID_ARTIFACT_SUFFIX": "service",
        "KATYDID_BASE_REF": "a" * 40,
        "KATYDID_EVENT_NAME": "pull_request",
        "KATYDID_EXPECTED_TARGET_REF": "b" * 40,
        "KATYDID_PLATFORM_REF": "c" * 40,
        "KATYDID_PROFILE": "quality.yaml",
        "KATYDID_STAGE": "pull-request",
        "KATYDID_TARGET_REF": "b" * 40,
    }


def execute(
    script: str, directory: Path, environment: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=directory,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def test_embedded_input_guard_accepts_exact_bounded_inputs(tmp_path: Path) -> None:
    validation, _checkout = embedded_guards()
    result = execute(validation, tmp_path, validation_environment())
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"KATYDID_TARGET_REF": "d" * 40}, "target_ref must match"),
        ({"KATYDID_PROFILE": "../quality.yaml"}, "portable relative YAML"),
        ({"KATYDID_STAGE": "deployment"}, "stage must be"),
    ],
)
def test_embedded_input_guard_rejects_unsafe_or_stale_inputs(
    tmp_path: Path, changes: dict[str, str], message: str
) -> None:
    validation, _checkout = embedded_guards()
    environment = {**validation_environment(), **changes}
    result = execute(validation, tmp_path, environment)
    assert result.returncode != 0
    assert message in result.stderr


def make_repository(directory: Path, profile: str) -> str:
    directory.mkdir()
    subprocess.run(
        ["git", "init", "--initial-branch=main"],
        cwd=directory,
        capture_output=True,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "Katydid Test"], cwd=directory, check=True)
    subprocess.run(
        ["git", "config", "user.email", "katydid@example.invalid"],
        cwd=directory,
        check=True,
    )
    (directory / "quality.yaml").write_text(profile, encoding="utf-8")
    subprocess.run(["git", "add", "quality.yaml"], cwd=directory, check=True)
    subprocess.run(
        ["git", "commit", "-m", "fixture"],
        cwd=directory,
        capture_output=True,
        check=True,
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=directory,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    ).stdout.strip()


def test_embedded_checkout_guard_rejects_changed_pull_request_profile(tmp_path: Path) -> None:
    _validation, checkout = embedded_guards()
    target_ref = make_repository(tmp_path / "target", "owner: pull-request\n")
    base_ref = make_repository(tmp_path / "policy", "owner: protected-base\n")
    platform_ref = make_repository(tmp_path / "platform", "owner: platform\n")
    environment = {
        **os.environ,
        "KATYDID_BASE_REF": base_ref,
        "KATYDID_EVENT_NAME": "pull_request",
        "KATYDID_PLATFORM_REF": platform_ref,
        "KATYDID_PROFILE": "quality.yaml",
        "KATYDID_TARGET_REF": target_ref,
    }

    result = execute(checkout, tmp_path, environment)

    assert result.returncode != 0
    assert "pull requests cannot change the executed quality profile" in result.stderr
