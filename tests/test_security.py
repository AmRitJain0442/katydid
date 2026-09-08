import json
import os
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from katydid import security
from katydid.evidence import Status, read_junit
from katydid.profile import make_plan
from katydid.runner import run_plan

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TOOLS = ROOT / ".katydid" / "security-tools"


@pytest.fixture
def real_tools(monkeypatch: pytest.MonkeyPatch) -> dict:
    directory = Path(os.environ.get("KATYDID_SECURITY_TOOLS", DEFAULT_TOOLS))
    metadata_path = directory / "setup.json"
    if not metadata_path.is_file():
        pytest.skip("run scripts/security/setup.py prepare for real scanner integration tests")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    monkeypatch.setenv("KATYDID_SECURITY_MANIFEST", str(metadata_path.resolve()))
    return metadata


def commit_all(root: Path) -> None:
    subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Security Test"], cwd=root, check=True)
    subprocess.run(
        ["git", "config", "user.email", "security@example.invalid"], cwd=root, check=True
    )
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "fixture"], cwd=root, check=True, capture_output=True)


def fake_manifest(tmp_path: Path, refreshed: datetime | None = None) -> Path:
    executable = tmp_path / "scanner.exe"
    executable.write_bytes(b"scanner")
    checksum = security._sha256(executable)
    cache = tmp_path / "cache" / "db"
    cache.mkdir(parents=True)
    database = cache / "trivy.db"
    metadata = cache / "metadata.json"
    database.write_bytes(b"database")
    metadata.write_bytes(b"metadata")
    value = {
        "schema_version": 1,
        "prepared_at": datetime.now(UTC).isoformat(),
        "versions": security.TOOL_VERSIONS,
        "executables": {name: str(executable.resolve()) for name in security.TOOL_VERSIONS},
        "executable_sha256": {name: checksum for name in security.TOOL_VERSIONS},
        "trivy_cache": str(cache.parent.resolve()),
        "trivy_database": {
            "refreshed_at": (refreshed or datetime.now(UTC)).isoformat(),
            "database_sha256": security._sha256(database),
            "metadata_sha256": security._sha256(metadata),
        },
    }
    path = tmp_path / "setup.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_parsers_discard_source_matches_descriptions_and_raw_secrets(tmp_path: Path) -> None:
    source = tmp_path / "app.py"
    source.write_text("safe = True\n", encoding="utf-8")
    semgrep_secret = "must-not-survive-semgrep"
    semgrep = security._semgrep_findings(
        {
            "results": [
                {
                    "check_id": "python-dangerous-eval",
                    "path": str(source),
                    "start": {"line": 7},
                    "extra": {"lines": semgrep_secret, "message": semgrep_secret},
                }
            ],
            "errors": [],
        },
        tmp_path,
    )
    dependency = security._dependency_findings(
        {
            "Results": [
                {
                    "Target": "requirements.txt",
                    "Vulnerabilities": [
                        {
                            "VulnerabilityID": "CVE-2026-1234",
                            "PkgName": "example",
                            "InstalledVersion": "1.0.0",
                            "Description": "must-not-survive-description",
                        }
                    ],
                }
            ]
        },
        tmp_path,
    )
    gitleaks_secret = "must-not-survive-gitleaks"
    gitleaks = security._gitleaks_findings(
        [
            {
                "RuleID": "generic-api-key",
                "File": "app.py",
                "StartLine": 9,
                "Secret": gitleaks_secret,
                "Match": gitleaks_secret,
            }
        ],
        tmp_path,
    )

    report = tmp_path / "junit.xml"
    security._junit(report, "static", [*semgrep, *dependency, *gitleaks], None)
    text = report.read_text(encoding="utf-8")
    assert semgrep_secret not in text
    assert gitleaks_secret not in text
    assert "must-not-survive-description" not in text
    assert "python-dangerous-eval" in text
    assert "CVE-2026-1234" in text
    assert "generic-api-key" in text
    assert read_junit(report).status == Status.FAILED


@pytest.mark.parametrize(
    ("parser", "value", "message"),
    [
        (security._semgrep_findings, {"results": [], "errors": [{}]}, "incomplete"),
        (
            security._dependency_findings,
            {"Results": [{"Vulnerabilities": "invalid"}]},
            "invalid vulnerabilities",
        ),
        (security._gitleaks_findings, {"not": "a-list"}, "invalid structure"),
    ],
)
def test_malformed_or_incomplete_scanner_results_fail_closed(
    tmp_path: Path, parser, value, message: str
) -> None:
    with pytest.raises(security.SecurityCheckError, match=message):
        parser(value, tmp_path)


def test_scan_root_and_configs_cannot_escape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "rules.yaml"
    outside.write_text("rules: []\n", encoding="utf-8")
    monkeypatch.chdir(root)
    with pytest.raises(security.SecurityCheckError, match="scan root"):
        security._local_root("../repo")
    with pytest.raises(security.SecurityCheckError, match="inside the scan root"):
        security._local_file(root, "../rules.yaml", "rules")
    with pytest.raises(security.SecurityCheckError, match="inside the scan root"):
        security._local_file(root, "..\\rules.yaml", "rules")


@pytest.mark.skipif(os.name != "nt", reason="Windows 8.3 path aliases are platform-specific")
def test_local_file_canonicalizes_windows_short_path(tmp_path: Path) -> None:
    import ctypes

    root = tmp_path / "security-directory-with-a-long-name"
    root.mkdir()
    rules = root / "rules.yaml"
    rules.write_text("rules: []\n", encoding="utf-8")
    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetShortPathNameW(str(root), buffer, len(buffer))
    if length == 0 or buffer.value.casefold() == str(root).casefold():
        pytest.skip("8.3 short paths are unavailable on this volume")
    assert security._local_file(Path(buffer.value), "rules.yaml", "rules") == rules.resolve()


def test_snapshot_uses_tracked_files_and_requires_approved_new_files(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    (root / "tracked.py").write_text("tracked = True\n", encoding="utf-8")
    commit_all(root)
    (root / "tracked.py").write_text("tracked = 'modified'\n", encoding="utf-8")
    (root / "generated.py").write_text("generated = True\n", encoding="utf-8")
    (root / "ignored.txt").write_text("host credential must stay outside\n", encoding="utf-8")
    destination = tmp_path / "snapshot"
    destination.mkdir()

    with pytest.raises(security.SecurityCheckError, match="untracked files"):
        security._tracked_snapshot(root, destination, [])

    security._tracked_snapshot(root, destination, ["generated.py"])
    assert (destination / "tracked.py").read_text(encoding="utf-8") == "tracked = 'modified'\n"
    assert (destination / "generated.py").is_file()
    assert not (destination / "ignored.txt").exists()


def test_manifest_hash_and_database_freshness_are_fail_closed(tmp_path: Path) -> None:
    manifest = fake_manifest(tmp_path)
    assert security._manifest("static", manifest, 72).executable.name == "scanner.exe"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["executable_sha256"]["semgrep"] = "0" * 64
    manifest.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(security.SecurityCheckError, match="checksum"):
        security._manifest("static", manifest, 72)

    stale_root = tmp_path / "stale"
    stale_root.mkdir()
    stale = fake_manifest(stale_root, datetime.now(UTC) - timedelta(hours=73))
    with pytest.raises(security.SecurityCheckError, match="older"):
        security._manifest("dependencies", stale, 72)


def test_scanner_result_size_is_bounded_while_process_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = tmp_path / "result.json"
    result.write_bytes(b"12345")
    monkeypatch.setattr(security, "MAX_RESULT_BYTES", 4)
    with pytest.raises(security.SecurityCheckError, match="exceeded 10 MiB"):
        security._check_result_bound(result)


def test_missing_scanner_emits_sanitized_errored_junit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "rules.yaml").write_text("credential-marker-that-must-not-leak", encoding="utf-8")
    report = tmp_path / "report.xml"
    assert (
        security.main(
            [
                "static",
                "--config",
                "rules.yaml",
                "--manifest",
                str(tmp_path / "missing-credential-marker-that-must-not-leak.json"),
                "--report",
                str(report),
            ]
        )
        == 2
    )
    output = capsys.readouterr()
    assert "credential-marker" not in output.out + output.err + report.read_text(encoding="utf-8")
    evidence = read_junit(report)
    assert evidence.status == Status.FAILED
    assert evidence.errors == 1


def test_real_clean_security_profile(tmp_path: Path, real_tools: dict) -> None:
    source = ROOT / "examples" / "security-service"
    repository = tmp_path / "security-service"
    shutil.copytree(source, repository)
    commit_all(repository)
    profile = repository / "quality.yaml"
    run = run_plan(make_plan(profile, "pull-request"), tmp_path / "runs")
    assert run.gate.passed
    assert [result.status for result in run.results] == [Status.PASSED] * 3
    assert all(result.evidence and result.evidence.tests == 1 for result in run.results)


def test_real_scanners_produce_failures_without_exposing_matches(
    tmp_path: Path, real_tools: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    semgrep_root = tmp_path / "semgrep"
    semgrep_root.mkdir()
    (semgrep_root / "bad.py").write_text(
        "def parse(value):\n    return eval(value)\n", encoding="utf-8"
    )
    rules = semgrep_root / "rules.yaml"
    rules.write_text(
        "rules:\n"
        "  - id: real-eval-finding\n"
        "    languages: [python]\n"
        "    severity: ERROR\n"
        "    message: unsafe evaluation\n"
        "    pattern: eval(...)\n",
        encoding="utf-8",
    )
    gitleaks_root = tmp_path / "gitleaks"
    gitleaks_root.mkdir()
    marker = "KATYDID_TEST_" + "SECRET_7F4C9D2A6B8E1A3C"
    (gitleaks_root / "settings.txt").write_text(f"token={marker}\n", encoding="utf-8")
    (gitleaks_root / "gitleaks.toml").write_text(
        'title = "Real scanner test"\n'
        "[[rules]]\n"
        'id = "katydid-test-secret"\n'
        'description = "Test credential marker"\n'
        "regex = '''KATYDID_TEST_SECRET_[A-Z0-9]{16}'''\n"
        'keywords = ["katydid_test_secret_"]\n',
        encoding="utf-8",
    )
    trivy_root = tmp_path / "trivy"
    trivy_root.mkdir()
    (trivy_root / "requirements.txt").write_text("urllib3==1.26.5\n", encoding="utf-8")
    commit_all(tmp_path)
    monkeypatch.chdir(tmp_path)
    semgrep_report = tmp_path / "semgrep.xml"
    assert security.scan("static", "semgrep", semgrep_report, 120, config="rules.yaml") == 1
    gitleaks_report = tmp_path / "gitleaks.xml"
    assert security.scan("secrets", "gitleaks", gitleaks_report, 120, config="gitleaks.toml") == 1
    trivy_report = tmp_path / "trivy.xml"
    assert security.scan("dependencies", "trivy", trivy_report, 180) == 1

    for report in (semgrep_report, gitleaks_report, trivy_report):
        evidence = read_junit(report)
        assert evidence.status == Status.FAILED
        assert evidence.failures >= 1
    assert marker not in gitleaks_report.read_text(encoding="utf-8")
