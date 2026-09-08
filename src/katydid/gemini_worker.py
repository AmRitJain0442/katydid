"""Single-call Google Gen AI worker kept behind a killable process boundary."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

MAX_REQUEST_BYTES = 8 * 1024 * 1024
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


def _read_request(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    if len(raw) > MAX_REQUEST_BYTES:
        raise ValueError("request-too-large")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("request-not-object")
    return value


def _write_response(path: Path, value: dict[str, Any]) -> None:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_RESPONSE_BYTES:
        encoded = b'{"ok":false,"error":{"category":"output","type":"ResponseTooLarge"}}'
    path.write_bytes(encoded)


def _safe_type(exc: BaseException) -> str:
    name = type(exc).__name__
    return name if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,99}", name) else "ProviderError"


def _category(exc: BaseException) -> str:
    name = f"{type(exc).__module__}.{type(exc).__name__}".casefold()
    if any(word in name for word in ("credential", "authentication", "unauthorized")):
        return "authentication"
    if any(word in name for word in ("permission", "forbidden")):
        return "authorization"
    if any(word in name for word in ("timeout", "transport", "connection", "network")):
        return "transport"
    if any(word in name for word in ("resourceexhausted", "ratelimit", "quota")):
        return "quota"
    if isinstance(exc, (KeyError, TypeError, ValueError)):
        return "invalid-response"
    return "provider"


def _required_string(request: dict[str, Any], name: str, maximum: int) -> str:
    value = request.get(name)
    if not isinstance(value, str) or not value or len(value) > maximum or "\x00" in value:
        raise ValueError(f"invalid-{name}")
    return value


def _client(request: dict[str, Any]) -> Any:
    from google import genai
    from google.genai import types

    timeout_seconds = request.get("timeout_seconds")
    if not isinstance(timeout_seconds, int) or not 1 <= timeout_seconds <= 900:
        raise ValueError("invalid-timeout")
    return genai.Client(
        vertexai=True,
        project=_required_string(request, "project", 100),
        location=_required_string(request, "location", 100),
        http_options=types.HttpOptions(api_version="v1", timeout=timeout_seconds * 1000),
    )


def _usage(response: Any) -> dict[str, int]:
    usage = getattr(response, "usage_metadata", None)
    if usage is None:
        return {}
    result: dict[str, int] = {}
    for name in (
        "prompt_token_count",
        "candidates_token_count",
        "total_token_count",
        "cached_content_token_count",
        "thoughts_token_count",
        "tool_use_prompt_token_count",
    ):
        value = getattr(usage, name, None)
        if isinstance(value, int) and value >= 0:
            result[name] = value
    return result


def _request_ids(response: Any) -> dict[str, str]:
    result: dict[str, str] = {}
    response_id = getattr(response, "response_id", None)
    if isinstance(response_id, str) and 0 < len(response_id) <= 500:
        result["response_id"] = response_id
    http_response = getattr(response, "sdk_http_response", None)
    headers = getattr(http_response, "headers", None)
    if headers is not None:
        lowered = {str(key).casefold(): str(value) for key, value in headers.items()}
        for name in ("x-request-id", "x-goog-request-id"):
            value = lowered.get(name)
            if value and len(value) <= 500:
                result[name.replace("-", "_")] = value
    return result


def _finished(response: Any) -> bool:
    candidates = getattr(response, "candidates", None)
    if not isinstance(candidates, list) or len(candidates) != 1:
        return False
    reason = getattr(candidates[0], "finish_reason", None)
    value = getattr(reason, "value", reason)
    return value == "STOP"


def _generate(request: dict[str, Any]) -> dict[str, Any]:
    from google.genai import types

    schema = request.get("schema")
    if not isinstance(schema, dict):
        raise ValueError("invalid-schema")
    max_output_tokens = request.get("max_output_tokens")
    if not isinstance(max_output_tokens, int) or not 64 <= max_output_tokens <= 65536:
        raise ValueError("invalid-output-budget")
    client = _client(request)
    try:
        response = client.models.generate_content(
            model=_required_string(request, "model", 200),
            contents=_required_string(request, "prompt", MAX_REQUEST_BYTES),
            config=types.GenerateContentConfig(
                system_instruction=_required_string(
                    request, "system_instruction", MAX_REQUEST_BYTES
                ),
                candidate_count=1,
                max_output_tokens=max_output_tokens,
                response_mime_type="application/json",
                response_json_schema=schema,
                tools=[],
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            ),
        )
        if not _finished(response):
            raise ValueError("response-not-complete")
        text = response.text
        if not isinstance(text, str) or not text:
            raise ValueError("missing-response-text")
        model_version = getattr(response, "model_version", None)
        metadata: dict[str, Any] = {
            "usage": _usage(response),
            "request_ids": _request_ids(response),
        }
        if isinstance(model_version, str) and 0 < len(model_version) <= 500:
            metadata["model_version"] = model_version
        return {"ok": True, "text": text, "metadata": metadata}
    finally:
        client.close()


def _doctor(request: dict[str, Any]) -> dict[str, Any]:
    from google.genai import types

    client = _client(request)
    try:
        response = client.models.generate_content(
            model=_required_string(request, "model", 200),
            contents="Return a JSON object whose ready field is true.",
            config=types.GenerateContentConfig(
                candidate_count=1,
                max_output_tokens=256,
                response_mime_type="application/json",
                response_json_schema={
                    "type": "object",
                    "properties": {"ready": {"type": "boolean"}},
                    "required": ["ready"],
                    "additionalProperties": False,
                },
                tools=[],
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )
        if not _finished(response):
            raise ValueError("doctor-response-not-complete")
        parsed = json.loads(response.text)
        if parsed != {"ready": True}:
            raise ValueError("doctor-response-invalid")
        return {
            "ok": True,
            "available": True,
            "provider": "gemini",
            "type": "vertex-ai",
            "model": _required_string(request, "model", 200),
            "project": _required_string(request, "project", 100),
            "location": _required_string(request, "location", 100),
        }
    finally:
        client.close()


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 3 or args[0] not in ("generate", "doctor"):
        return 2
    output = Path(args[2])
    try:
        request = _read_request(Path(args[1]))
        result = _generate(request) if args[0] == "generate" else _doctor(request)
        _write_response(output, result)
        return 0
    except BaseException as exc:
        try:
            _write_response(
                output,
                {"ok": False, "error": {"category": _category(exc), "type": _safe_type(exc)}},
            )
        except BaseException:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
