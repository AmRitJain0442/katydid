import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import katydid.gemini as gemini
import katydid.gemini_worker as worker
from katydid.ai import AIError, CodexProvider, Diagnosis, Repair, create_provider
from katydid.fleet import AIConfig
from katydid.gemini import GeminiProvider, gemini_doctor

STUB = r"""
import json
import os
import sys
import time

mode, request_path, response_path = sys.argv[1:]
request = json.loads(open(request_path, encoding="utf-8").read())
behavior = os.environ.get("KATYDID_GEMINI_STUB_MODE", "valid")
if behavior == "sleep":
    time.sleep(60)
if behavior == "noisy":
    print("x" * 2000, flush=True)
if mode == "doctor":
    response = {
        "ok": True,
        "available": True,
        "provider": "gemini",
        "type": "vertex-ai",
        "model": request["model"],
        "project": request["project"],
        "location": request["location"],
    }
else:
    diagnosis = {
        "summary": "A concrete defect",
        "repairable": True,
        "evidence": ["unit failed"],
    }
    text = json.dumps(diagnosis)
    if behavior == "invalid-schema":
        text = json.dumps({**diagnosis, "unexpected": True})
    elif behavior == "oversized":
        text = json.dumps({**diagnosis, "summary": "x" * 2000})
    response = {
        "ok": True,
        "text": text,
        "metadata": {
            "usage": {"prompt_token_count": 17, "total_token_count": 29},
            "request_ids": {"response_id": "response-123"},
            "model_version": "gemini-2.5-flash-001",
        },
    }
open(response_path, "w", encoding="utf-8").write(json.dumps(response))
"""


def config(**changes):
    values = {
        "provider": "gemini",
        "model": "gemini-2.5-flash",
        "vertex_project": "sample-project-12345",
        "timeout_seconds": 10,
        "max_calls_per_task": 2,
        "max_context_bytes": 1000,
        "max_output_bytes": 1000,
        "max_output_tokens": 256,
    }
    values.update(changes)
    return AIConfig(**values)


@pytest.fixture
def stub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "gemini_stub.py"
    path.write_text(STUB, encoding="utf-8")
    monkeypatch.setattr(gemini, "_worker_command", lambda: [sys.executable, str(path)])
    return path


def test_gemini_configuration_is_explicit_and_has_no_credential_field() -> None:
    with pytest.raises(ValidationError, match="explicit model"):
        AIConfig(provider="gemini", vertex_project="sample-project-12345")
    with pytest.raises(ValidationError, match="vertex_project"):
        AIConfig(provider="gemini", model="gemini-2.5-flash")
    with pytest.raises(ValidationError, match="Codex-only"):
        config(reasoning="low")
    with pytest.raises(ValidationError, match="Extra inputs"):
        config(credentials="secret.json")
    codex = AIConfig()
    assert codex.provider == "codex"
    assert AIConfig.model_validate(codex.model_dump()) == codex
    gemini_config = config()
    assert AIConfig.model_validate(gemini_config.model_dump()) == gemini_config


def test_factory_preserves_codex_default_and_selects_gemini(tmp_path: Path) -> None:
    assert isinstance(create_provider(AIConfig(), tmp_path / "codex"), CodexProvider)
    assert isinstance(create_provider(config(), tmp_path / "gemini"), GeminiProvider)


def test_valid_response_is_strictly_parsed_and_receipted(
    tmp_path: Path, stub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    credential_path = tmp_path / "do-not-record-service-account.json"
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(credential_path))
    provider = GeminiProvider(config(), tmp_path / "ai")
    result = provider.ask(
        "diagnosis", {"files": {"app.py": "broken"}}, Diagnosis, threading.Event()
    )

    assert result == Diagnosis(
        summary="A concrete defect", repairable=True, evidence=["unit failed"]
    )
    folder = tmp_path / "ai" / "01-diagnosis"
    receipt = json.loads((folder / "receipt.json").read_text(encoding="utf-8"))
    assert receipt == {
        "provider": "gemini",
        "model": "gemini-2.5-flash",
        "role": "diagnosis",
        "call_id": receipt["call_id"],
        "duration_seconds": receipt["duration_seconds"],
        "response_validated": True,
        "usage": {"prompt_token_count": 17, "total_token_count": 29},
        "request_ids": {"response_id": "response-123"},
        "model_version": "gemini-2.5-flash-001",
    }
    assert len(receipt["call_id"]) == 32
    serialized = "".join(
        path.read_text(encoding="utf-8")
        for path in (folder / "request.json", folder / "invocation.json", folder / "receipt.json")
    )
    assert str(credential_path) not in serialized
    request = json.loads((folder / "request.json").read_text(encoding="utf-8"))
    assert request["role"] == "diagnosis"
    assert request["project"] == "sample-project-12345"
    assert "untrusted evidence" in request["system_instruction"]
    assert request["schema"] == Diagnosis.model_json_schema()


@pytest.mark.parametrize(
    ("behavior", "message"),
    [("invalid-schema", "invalid structured output"), ("oversized", "oversized structured")],
)
def test_invalid_and_oversized_responses_fail_closed(
    tmp_path: Path,
    stub: Path,
    monkeypatch: pytest.MonkeyPatch,
    behavior: str,
    message: str,
) -> None:
    monkeypatch.setenv("KATYDID_GEMINI_STUB_MODE", behavior)
    provider = GeminiProvider(config(max_output_bytes=1000), tmp_path / behavior)
    with pytest.raises(AIError, match=message):
        provider.ask("diagnosis", {}, Diagnosis, threading.Event())
    assert not (tmp_path / behavior / "01-diagnosis" / "receipt.json").exists()


def test_context_call_role_and_precancel_budgets(tmp_path: Path, stub: Path) -> None:
    provider = GeminiProvider(config(), tmp_path / "calls")
    for _ in range(2):
        provider.ask("diagnosis", {}, Diagnosis, threading.Event())
    with pytest.raises(AIError, match="call budget exhausted"):
        provider.ask("diagnosis", {}, Diagnosis, threading.Event())

    context_provider = GeminiProvider(config(), tmp_path / "context")
    with pytest.raises(AIError, match="context exceeds"):
        context_provider.ask("diagnosis", {"value": "é" * 1000}, Diagnosis, threading.Event())
    assert context_provider.calls == 0

    with pytest.raises(AIError, match="role is invalid"):
        GeminiProvider(config(), tmp_path / "role").ask(
            "../diagnosis", {}, Diagnosis, threading.Event()
        )
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(AIError, match="cancelled before launch"):
        GeminiProvider(config(), tmp_path / "cancelled").ask("diagnosis", {}, Diagnosis, cancelled)


def test_inflight_cancel_hard_stops_worker(
    tmp_path: Path, stub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KATYDID_GEMINI_STUB_MODE", "sleep")
    cancel = threading.Event()
    timer = threading.Timer(0.2, cancel.set)
    timer.start()
    try:
        with pytest.raises(AIError, match="AI call cancelled"):
            GeminiProvider(config(), tmp_path / "cancel").ask("diagnosis", {}, Diagnosis, cancel)
    finally:
        timer.cancel()
        timer.join()


def test_timeout_and_combined_log_budget_stop_worker(
    tmp_path: Path, stub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KATYDID_GEMINI_STUB_MODE", "sleep")
    ticks = iter((100.0, 100.0, 111.0))
    monkeypatch.setattr(gemini.time, "monotonic", lambda: next(ticks))
    with pytest.raises(AIError, match="exceeded its timeout"):
        GeminiProvider(config(), tmp_path / "timeout").ask(
            "diagnosis", {}, Diagnosis, threading.Event()
        )

    monkeypatch.undo()
    monkeypatch.setattr(gemini, "_worker_command", lambda: [sys.executable, str(stub)])
    monkeypatch.setenv("KATYDID_GEMINI_STUB_MODE", "noisy")
    monkeypatch.setattr(gemini, "MAX_LOG_BYTES", 128)
    with pytest.raises(AIError, match="output exceeded"):
        GeminiProvider(config(), tmp_path / "noisy").ask(
            "diagnosis", {}, Diagnosis, threading.Event()
        )
    assert (tmp_path / "noisy" / "01-diagnosis" / "stdout.log").stat().st_size <= 128


def test_doctor_returns_only_sanitized_metadata(tmp_path: Path, stub: Path) -> None:
    assert gemini_doctor(config(), tmp_path / "doctor") == {
        "available": True,
        "provider": "gemini",
        "type": "vertex-ai",
        "model": "gemini-2.5-flash",
        "project": "sample-project-12345",
        "location": "global",
    }


def test_worker_disables_tools_requires_complete_response_and_sanitizes_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = {}
    response = SimpleNamespace(
        text=json.dumps({"summary": "defect", "repairable": True, "evidence": ["failed test"]}),
        candidates=[SimpleNamespace(finish_reason=SimpleNamespace(value="STOP"))],
        usage_metadata=SimpleNamespace(prompt_token_count=11, total_token_count=19),
        response_id="response-456",
        model_version="gemini-version",
        sdk_http_response=None,
    )

    class Models:
        def generate_content(self, **kwargs):
            captured.update(kwargs)
            return response

    client = SimpleNamespace(models=Models(), close=lambda: None)
    monkeypatch.setattr(worker, "_client", lambda request: client)
    request = {
        "model": "gemini-2.5-flash",
        "project": "sample-project-12345",
        "location": "global",
        "timeout_seconds": 8,
        "prompt": "{}",
        "system_instruction": "system",
        "schema": Diagnosis.model_json_schema(),
        "max_output_tokens": 256,
    }
    result = worker._generate(request)
    assert result["ok"] is True
    assert captured["model"] == "gemini-2.5-flash"
    generation = captured["config"]
    assert generation.tools == []
    assert generation.automatic_function_calling.disable is True
    assert generation.response_json_schema == Diagnosis.model_json_schema()
    assert result["metadata"]["usage"] == {
        "prompt_token_count": 11,
        "total_token_count": 19,
    }

    response.candidates[0].finish_reason.value = "MAX_TOKENS"
    with pytest.raises(ValueError, match="not-complete"):
        worker._generate(request)

    def secret_failure(request):
        raise RuntimeError("private credential contents")

    monkeypatch.setattr(worker, "_client", secret_failure)
    request_path = tmp_path / "request.json"
    result_path = tmp_path / "result.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    assert worker.main(["generate", str(request_path), str(result_path)]) == 1
    serialized = result_path.read_text(encoding="utf-8")
    assert "private credential contents" not in serialized
    assert json.loads(serialized) == {
        "ok": False,
        "error": {"category": "provider", "type": "RuntimeError"},
    }


def test_repair_paths_are_checked_after_schema_validation(
    tmp_path: Path, stub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def bad_worker(mode, request, folder, timeout, cancel):
        return {
            "ok": True,
            "text": json.dumps(
                {"summary": "bad", "edits": [{"path": "../secret", "content": "x"}]}
            ),
            "metadata": {},
        }

    monkeypatch.setattr(gemini, "_run_worker", bad_worker)
    with pytest.raises(AIError, match="invalid repair path"):
        GeminiProvider(config(), tmp_path / "repair").ask("repair", {}, Repair, threading.Event())
