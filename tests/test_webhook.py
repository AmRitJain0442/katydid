import hashlib
import hmac
import http.client
import json
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http import HTTPStatus
from typing import Any

import pytest

import katydid.webhook as webhook
from katydid.events import MAX_BODY_BYTES, InvalidEvent


class RecordingIngestor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bytes]] = []
        self.error: Exception | None = None

    def ingest(self, event: str, delivery: str, body: bytes) -> dict[str, Any]:
        self.calls.append((event, delivery, body))
        if self.error is not None:
            raise self.error
        return {"status": "enqueued", "task_id": "task-1"}


@contextmanager
def running_server(
    ingestor: RecordingIngestor, secret: bytes = b"It's a Secret to Everybody"
) -> Iterator[tuple[str, int]]:
    server = webhook.make_webhook_server(ingestor, secret, port=0)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        host, port = server.server_address[:2]
        yield str(host), int(port)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def signature(secret: bytes, body: bytes) -> str:
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


def request(
    address: tuple[str, int],
    body: bytes,
    *,
    path: str = "/webhooks/github",
    signed: str | None = None,
) -> tuple[int, dict[str, Any]]:
    connection = http.client.HTTPConnection(*address, timeout=5)
    connection.request(
        "POST",
        path,
        body=body,
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": "push",
            "X-GitHub-Delivery": "delivery-1",
            "X-Hub-Signature-256": signed or signature(b"It's a Secret to Everybody", body),
        },
    )
    response = connection.getresponse()
    value = json.loads(response.read())
    connection.close()
    return response.status, value


def test_server_validates_official_sha256_vector_before_exact_byte_ingestion() -> None:
    ingestor = RecordingIngestor()
    body = b"Hello, World!"
    official = "sha256=757107ea0eb2509fc211221cce984b8a37570b6d7586c22c46f4379c8b043e17"
    with running_server(ingestor) as address:
        status, result = request(address, body, signed=official)

    assert status == HTTPStatus.ACCEPTED
    assert result == {"status": "enqueued", "task_id": "task-1"}
    assert ingestor.calls == [("push", "delivery-1", body)]


def test_bad_signature_unknown_path_and_ingestor_errors_do_not_echo_body() -> None:
    ingestor = RecordingIngestor()
    body = b'{"secret":"must-not-appear"}'
    with running_server(ingestor) as address:
        status, result = request(address, body, signed="sha256=" + "0" * 64)
        assert status == HTTPStatus.FORBIDDEN
        assert "must-not-appear" not in json.dumps(result)
        assert ingestor.calls == []

        status, _result = request(address, body, path="/webhooks/github?debug=true")
        assert status == HTTPStatus.NOT_FOUND
        assert ingestor.calls == []

        ingestor.error = InvalidEvent("must-not-appear")
        status, result = request(address, body)
        assert status == HTTPStatus.UNPROCESSABLE_ENTITY
        assert result == {"error": "Invalid GitHub event"}
        assert "must-not-appear" not in json.dumps(result)


def test_duplicate_headers_chunking_and_oversized_lengths_are_rejected_without_body() -> None:
    ingestor = RecordingIngestor()
    body = b"{}"
    with running_server(ingestor) as address:
        connection = http.client.HTTPConnection(*address, timeout=5)
        connection.putrequest("POST", "/webhooks/github")
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", str(len(body)))
        connection.putheader("X-GitHub-Event", "push")
        connection.putheader("X-GitHub-Event", "release")
        connection.putheader("X-GitHub-Delivery", "delivery-duplicate")
        connection.putheader("X-Hub-Signature-256", signature(b"It's a Secret to Everybody", body))
        connection.endheaders(body)
        response = connection.getresponse()
        assert response.status == HTTPStatus.BAD_REQUEST
        response.read()
        connection.close()

        connection = http.client.HTTPConnection(*address, timeout=5)
        connection.putrequest("POST", "/webhooks/github")
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Transfer-Encoding", "chunked")
        connection.putheader("X-GitHub-Event", "push")
        connection.putheader("X-GitHub-Delivery", "delivery-chunked")
        connection.putheader("X-Hub-Signature-256", signature(b"secret", body))
        connection.endheaders()
        response = connection.getresponse()
        assert response.status == HTTPStatus.BAD_REQUEST
        response.read()
        connection.close()

        connection = http.client.HTTPConnection(*address, timeout=5)
        connection.putrequest("POST", "/webhooks/github")
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", str(MAX_BODY_BYTES + 1))
        connection.putheader("X-GitHub-Event", "push")
        connection.putheader("X-GitHub-Delivery", "delivery-large")
        connection.putheader("X-Hub-Signature-256", "sha256=" + "0" * 64)
        connection.endheaders()
        response = connection.getresponse()
        assert response.status == HTTPStatus.REQUEST_ENTITY_TOO_LARGE
        response.read()
        connection.close()

    assert ingestor.calls == []


def test_incomplete_request_body_times_out_boundedly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(webhook, "REQUEST_TIMEOUT_SECONDS", 0.05)
    ingestor = RecordingIngestor()
    with running_server(ingestor) as address:
        with socket.create_connection(address, timeout=5) as connection:
            connection.settimeout(5)
            connection.sendall(
                b"POST /webhooks/github HTTP/1.1\r\n"
                + f"Host: {address[0]}:{address[1]}\r\n".encode()
                + b"Content-Type: application/json\r\n"
                + b"Content-Length: 100\r\n"
                + b"X-GitHub-Event: push\r\n"
                + b"X-GitHub-Delivery: delivery-timeout\r\n"
                + b"X-Hub-Signature-256: sha256="
                + b"0" * 64
                + b"\r\n\r\n{"
            )
            response = b""
            while b"\r\n\r\n" not in response:
                response += connection.recv(4096)
    assert response.startswith(b"HTTP/1.1 408 Request Timeout")
    assert ingestor.calls == []


def test_slow_headers_are_bounded_and_listener_is_loopback_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(webhook, "REQUEST_TIMEOUT_SECONDS", 0.05)
    with running_server(RecordingIngestor()) as address:
        with socket.create_connection(address, timeout=5) as connection:
            connection.settimeout(1)
            connection.sendall(b"POST /webhooks/github HTTP/1.1\r\nHost: localhost")
            started = time.monotonic()
            assert connection.recv(4096) == b""
            assert time.monotonic() - started < 0.5

    with pytest.raises(ValueError, match="loopback"):
        webhook.make_webhook_server(RecordingIngestor(), b"secret", "0.0.0.0", 0)


@pytest.mark.parametrize("secret", [b"", "text", None])
def test_server_rejects_invalid_secrets(secret: Any) -> None:
    with pytest.raises(ValueError, match="secret"):
        webhook.make_webhook_server(RecordingIngestor(), secret)
