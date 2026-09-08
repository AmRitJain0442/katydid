import subprocess
import sys
from pathlib import Path

import pytest

from katydid.evidence import Status, read_junit
from katydid.fleet import load_fleet
from katydid.onboarding import OnboardingError, onboard
from katydid.profile import load_profile, make_plan


def git(repository: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def repository(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "source"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Onboarding Test")
    git(root, "config", "user.email", "onboarding@example.invalid")
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-m", "fixture")
    return root


def python_project(tmp_path: Path) -> Path:
    return repository(
        tmp_path,
        {
            "pyproject.toml": """[project]
name = "orders_api"
version = "0.1.0"
dependencies = []

[dependency-groups]
dev = ["pytest>=8", "ruff>=0.11", "mypy>=1.15"]

[tool.pytest.ini_options]
testpaths = ["tests"]

[tool.ruff]
target-version = "py312"

[tool.mypy]
strict = true
""",
            "src/orders_api.py": "def total() -> int:\n    return 2\n",
            "tests/test_orders.py": "def test_total():\n    assert 1 + 1 == 2\n",
            "README.md": "# Orders API\n",
            ".env.example": "THIS_IS_NOT_CONTEXT=true\n",
        },
    )


def test_generates_valid_review_first_bundle_without_running_project_code(tmp_path: Path) -> None:
    source = python_project(tmp_path)
    marker = source / "PROJECT_COMMAND_RAN"
    (source / "tests" / "test_orders.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n",
        encoding="utf-8",
    )
    destination = tmp_path / "central" / "orders-onboarding"

    result = onboard(source=str(source), destination=destination, owner="payments-team")

    assert result.repository_id == "source"
    assert result.checks == ("python-tests", "python-lint", "python-types")
    assert not marker.exists()
    assert sorted(path.name for path in destination.iterdir()) == [
        "ONBOARDING_REVIEW.md",
        "fleet.yaml",
        "quality.yaml",
    ]
    profile, _ = load_profile(destination / "quality.yaml")
    assert profile.owner == "payments-team"
    assert profile.checks[0].argv == [
        "{python}",
        "-m",
        "pytest",
        "-q",
        "--junitxml={report}",
    ]
    assert set(profile.checks[0].stages) == {
        "pull-request",
        "merge",
        "nightly",
        "release",
    }
    fleet, _ = load_fleet(destination / "fleet.yaml")
    registered = fleet.repository("source")
    assert registered.source == str(source)
    assert registered.editable_paths == []
    assert registered.delivery.mode == "none"
    assert Path(fleet.state_directory).is_relative_to(destination)
    assert not Path(fleet.state_directory).is_relative_to(source)
    assert ".env.example" not in registered.context_paths
    assert set(registered.required_checks) == set(result.checks)
    for stage in ("pull-request", "merge", "nightly", "release"):
        assert [check.id for check in make_plan(destination / "quality.yaml", stage, source).checks]
    review = (destination / "ONBOARDING_REVIEW.md").read_text(encoding="utf-8")
    assert "did not install dependencies" in review
    assert "No editable paths" in review


def test_inspects_committed_revision_instead_of_dirty_worktree(tmp_path: Path) -> None:
    source = python_project(tmp_path)
    committed = git(source, "rev-parse", "HEAD")
    (source / "pyproject.toml").write_text("this is not toml", encoding="utf-8")

    result = onboard(str(source), tmp_path / "review")

    assert result.inspected_commit == committed
    assert "python-tests" in result.checks


def test_preserves_existing_profile_and_derives_stage_policy(tmp_path: Path) -> None:
    profile = """schema_version: 1
repository: protected-orders
owner: existing-team
checks:
  - id: core
    kind: test
    argv: ['{python}', -m, pytest, '--junitxml={report}']
    stages: [pull-request, merge, nightly, release]
  - id: release-contract
    kind: command
    argv: ['{python}', scripts/release_contract.py]
    stages: [release]
"""
    source = repository(
        tmp_path,
        {
            "quality.yaml": profile,
            "pyproject.toml": "[project]\nname='existing'\nversion='1.0.0'\n",
            "tests/test_app.py": "def test_app(): assert True\n",
            "scripts/release_contract.py": "raise SystemExit(0)\n",
        },
    )

    result = onboard(str(source), tmp_path / "review", owner="ignored-for-existing")

    assert result.repository_id == "protected-orders"
    assert (tmp_path / "review" / "quality.yaml").read_text(encoding="utf-8") == profile
    fleet, _ = load_fleet(tmp_path / "review" / "fleet.yaml")
    registered = fleet.repository("protected-orders")
    assert registered.required_checks == {"core": "test"}
    assert registered.required_checks_by_stage == {"release": {"release-contract": "command"}}
    assert "quality.yaml" in registered.context_paths
    assert "preserved byte-for-byte" in (tmp_path / "review" / "ONBOARDING_REVIEW.md").read_text(
        encoding="utf-8"
    )


def test_existing_profile_can_support_only_declared_stages(tmp_path: Path) -> None:
    profile = """schema_version: 1
repository: pull-request-only
owner: existing-team
checks:
  - id: core
    kind: command
    argv: ['{python}', -m, unittest]
    stages: [pull-request]
"""
    source = repository(tmp_path, {"quality.yaml": profile, "app.py": "VALUE = 1\n"})

    result = onboard(str(source), tmp_path / "review")

    fleet, _ = load_fleet(tmp_path / "review" / "fleet.yaml")
    assert fleet.repositories[0].required_checks == {"core": "command"}
    assert any("merge, nightly, release" in gap for gap in result.gaps)


def test_detects_vitest_with_direct_portable_junit_argv(tmp_path: Path) -> None:
    source = repository(
        tmp_path,
        {
            "package.json": (
                '{"scripts":{"test":"vitest run","lint":"eslint ."},'
                '"devDependencies":{"vitest":"3.2.0","eslint":"9.0.0"}}'
            )
        },
    )

    result = onboard(str(source), tmp_path / "review")

    assert result.checks == ("node-tests",)
    profile, _ = load_profile(tmp_path / "review" / "quality.yaml")
    assert profile.checks[0].kind == "test"
    assert profile.checks[0].argv == [
        "node",
        "node_modules/vitest/vitest.mjs",
        "run",
        "--reporter=junit",
        "--outputFile={report}",
    ]
    assert any("scripts.lint was not promoted" in gap for gap in result.gaps)


@pytest.mark.parametrize(
    ("files", "gap"),
    [
        ({"package.json": '{"scripts":{"test":"jest"}}'}, "scripts.test"),
        ({"Cargo.toml": "[package]\nname='sample'\nversion='0.1.0'\n"}, "Cargo tests"),
        ({"go.mod": "module example.invalid/sample\n\ngo 1.24\n"}, "Go tests"),
    ],
)
def test_unsupported_test_reporters_are_gaps_not_command_gates(
    tmp_path: Path, files: dict[str, str], gap: str
) -> None:
    source = repository(tmp_path, files)

    with pytest.raises(OnboardingError, match=gap):
        onboard(str(source), tmp_path / "review")

    assert not (tmp_path / "review").exists()


def test_generates_real_unittest_junit_adapter_check(tmp_path: Path) -> None:
    source = repository(
        tmp_path,
        {
            "pyproject.toml": "[project]\nname='sample'\nversion='0.1.0'\n",
            "tests/test_sample.py": "import unittest\n",
        },
    )

    result = onboard(str(source), tmp_path / "review")

    profile, _ = load_profile(tmp_path / "review" / "quality.yaml")
    assert result.checks == ("python-tests",)
    assert profile.checks[0].kind == "test"
    assert profile.checks[0].argv == [
        "{python}",
        "-I",
        "-m",
        "katydid.onboarding",
        "unittest-junit",
        "{report}",
        "tests",
    ]


@pytest.mark.parametrize(
    ("test_source", "expected_code", "expected_status", "expected_tests"),
    [
        (
            "import unittest\nimport application\nclass Example(unittest.TestCase):\n"
            "    def test_ok(self): self.assertEqual(application.VALUE, 4)\n",
            0,
            Status.PASSED,
            1,
        ),
        (
            "import unittest\nclass Example(unittest.TestCase):\n"
            "    def test_bad(self): self.assertEqual(2 + 2, 5)\n",
            1,
            Status.FAILED,
            1,
        ),
        ("# zero tests\n", 1, Status.NO_TESTS, 0),
    ],
)
def test_unittest_adapter_records_actual_result_events(
    tmp_path: Path,
    test_source: str,
    expected_code: int,
    expected_status: Status,
    expected_tests: int,
) -> None:
    project = tmp_path / "adapter-project"
    tests = project / "tests"
    tests.mkdir(parents=True)
    (project / "application.py").write_text("VALUE = 4\n", encoding="utf-8")
    (tests / "test_sample.py").write_text(test_source, encoding="utf-8")
    report = tmp_path / "junit.xml"

    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-m",
            "katydid.onboarding",
            "unittest-junit",
            str(report),
            "tests",
        ],
        cwd=project,
        check=False,
        capture_output=True,
        text=True,
    )

    evidence = read_junit(report)
    assert result.returncode == expected_code
    assert evidence.status == expected_status
    assert evidence.tests == expected_tests


def test_remote_source_uses_bare_clone_and_normalized_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = python_project(tmp_path)
    bare = tmp_path / "upstream.git"
    subprocess.run(
        ["git", "clone", "--bare", str(source), str(bare)], check=True, capture_output=True
    )
    import katydid.onboarding as module

    original = module._run_git
    clone_argv: list[str] = []

    def fake_run(argv: list[str], *, max_bytes: int = 64 * 1024) -> bytes:
        if argv[:2] == ["git", "clone"]:
            clone_argv.extend(argv)
            rewritten = [*argv]
            url_index = rewritten.index("--") + 1
            rewritten[url_index] = str(bare)
            return original(rewritten, max_bytes=max_bytes)
        return original(argv, max_bytes=max_bytes)

    monkeypatch.setattr(module, "_run_git", fake_run)

    result = onboard(
        "https://github.com/Example/Orders.git",
        tmp_path / "review",
        base_branch="main",
    )

    assert result.source == "https://github.com/Example/Orders.git"
    assert "--bare" in clone_argv
    assert "--single-branch" in clone_argv
    assert not (tmp_path / "review" / ".inspection.git").exists()


def test_refuses_overwrite_unsupported_project_and_unsafe_locations(tmp_path: Path) -> None:
    source = repository(tmp_path, {"README.md": "# No checks\n"})
    existing = tmp_path / "existing"
    existing.mkdir()
    (existing / "keep.txt").write_text("keep", encoding="utf-8")

    with pytest.raises(OnboardingError, match="already exists"):
        onboard(str(source), existing)
    assert (existing / "keep.txt").read_text(encoding="utf-8") == "keep"
    with pytest.raises(OnboardingError, match="No structured"):
        onboard(str(source), tmp_path / "unsupported")
    assert not (tmp_path / "unsupported").exists()
    with pytest.raises(OnboardingError, match="outside the source"):
        onboard(str(source), source / "central")
    assert not (source / "central").exists()


@pytest.mark.parametrize(
    "source",
    [
        "http://github.com/owner/repo",
        "https://user@github.com/owner/repo",
        "https://github.com/owner/repo?token=secret",
        "ssh://github.com/owner/repo",
        "git@github.com:owner/repo.git",
    ],
)
def test_rejects_untrusted_remote_source_forms(tmp_path: Path, source: str) -> None:
    with pytest.raises(OnboardingError, match="credential-free HTTPS GitHub"):
        onboard(source, tmp_path / "review")


def test_malformed_metadata_fails_atomically(tmp_path: Path) -> None:
    source = repository(tmp_path, {"package.json": "[]"})

    with pytest.raises(OnboardingError, match="Expected an object"):
        onboard(str(source), tmp_path / "review")

    assert not (tmp_path / "review").exists()


def test_does_not_remove_unrelated_sibling_when_generation_fails(tmp_path: Path) -> None:
    source = repository(tmp_path, {"README.md": "# No checks\n"})
    sibling = tmp_path / "important"
    sibling.mkdir()
    (sibling / "data.txt").write_text("preserve", encoding="utf-8")

    with pytest.raises(OnboardingError):
        onboard(str(source), tmp_path / "review")

    assert (sibling / "data.txt").read_text(encoding="utf-8") == "preserve"
