import json
from types import SimpleNamespace

import pytest
from test_runner import make_profile

from katydid import service
from katydid.profile import make_plan
from katydid.runner import run_plan


@pytest.mark.parametrize("available", [True, False])
def test_gemini_doctor_uses_selected_authentication_without_codex(
    tmp_path, monkeypatch, capsys, available
):
    from katydid import gemini

    ai = SimpleNamespace(provider="gemini")
    config = SimpleNamespace(ai=ai, repositories=[])
    monkeypatch.setattr(service, "load_fleet", lambda _path: (config, "digest"))

    def no_codex(_config):
        raise AssertionError("Gemini diagnostics must not require Codex")

    def probe(actual, directory):
        assert actual is ai
        assert directory.is_dir()
        return {"available": available, "provider": "gemini"}

    def git_probe(argv, **_kwargs):
        assert argv == ["git", "--version"]
        return SimpleNamespace(returncode=0, stdout="git version test", stderr="")

    monkeypatch.setattr(service, "codex_command", no_codex)
    monkeypatch.setattr(gemini, "gemini_doctor", probe)
    monkeypatch.setattr(service.subprocess, "run", git_probe)
    assert service.doctor(tmp_path / "fleet.yaml") == (0 if available else 1)
    result = json.loads(capsys.readouterr().out)
    assert result["passed"] is available
    assert result["checks"]["ai_auth"]["provider"] == "gemini"
    assert "codex" not in result["checks"]


def test_repository_process_does_not_inherit_google_model_credentials(tmp_path, monkeypatch):
    names = ("GOOGLE_APPLICATION_CREDENTIALS", "GEMINI_API_KEY", "GOOGLE_API_KEY")
    for name in names:
        monkeypatch.setenv(name, "private-model-credential-marker")
    monkeypatch.setenv("KATYDID_TEST_APPLICATION_SETTING", "preserved")
    script = (
        "import os\n"
        f"assert all(name not in os.environ for name in {names!r})\n"
        "assert os.environ['KATYDID_TEST_APPLICATION_SETTING'] == 'preserved'\n"
        "print('model credentials excluded')\n"
    )
    profile = make_profile(tmp_path, [script], kind="command")
    run = run_plan(make_plan(profile, "pull-request"))
    assert run.gate.passed
    for path in run.directory.rglob("*.log"):
        assert "private-model-credential-marker" not in path.read_text(encoding="utf-8")
