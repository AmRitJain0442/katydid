"""Small signed GitHub webhook HTTP boundary."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from katydid.events import MAX_BODY_BYTES, EventError, EventRace, InvalidEvent, UnknownRepository
from katydid.store import EventConflict

REQUEST_TIMEOUT_SECONDS = 10
_SIGNATURE = re.compile(r"^sha256=[0-9a-f]{64}$")


class _RequestError(Exception):
    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class WebhookServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False


def make_webhook_server(
    ingestor: Any,
    secret: bytes,
    host: str = "127.0.0.1",
    port: int = 8766,
) -> ThreadingHTTPServer:
    """Construct a loopback-oriented bounded webhook server without starting it."""
    if not isinstance(secret, bytes) or not secret:
        raise ValueError("Webhook secret must be non-empty bytes")
    if not isinstance(host, str) or host.casefold() not in {"127.0.0.1", "localhost"}:
        raise ValueError("Webhook host must be loopback (127.0.0.1 or localhost)")
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("Webhook port must be between 0 and 65535")
    token = bytes(secret)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(REQUEST_TIMEOUT_SECONDS)

        def log_message(self, _format: str, *args: object) -> None:
            del args

        def handle_expect_100(self) -> bool:
            self._reply(HTTPStatus.EXPECTATION_FAILED, {"error": "Expect is not supported"})
            return False

        def _reply(self, status: HTTPStatus, value: dict[str, Any]) -> None:
            body = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True

        def _headers(self) -> tuple[str, str, str, int]:
            counts: dict[str, int] = {}
            for name, _value in self.headers.raw_items():
                folded = name.casefold()
                counts[folded] = counts.get(folded, 0) + 1
            if any(count != 1 for count in counts.values()):
                raise _RequestError(HTTPStatus.BAD_REQUEST, "Duplicate HTTP headers are rejected")
            if self.headers.get("Transfer-Encoding") is not None:
                raise _RequestError(
                    HTTPStatus.BAD_REQUEST, "Transfer-Encoding is not accepted for webhooks"
                )
            content_length = self.headers.get("Content-Length")
            if (
                content_length is None
                or len(content_length) > 7
                or re.fullmatch(r"[0-9]+", content_length) is None
            ):
                raise _RequestError(
                    HTTPStatus.LENGTH_REQUIRED, "A valid Content-Length is required"
                )
            length = int(content_length)
            if length <= 0:
                raise _RequestError(HTTPStatus.BAD_REQUEST, "Webhook body must not be empty")
            if length > MAX_BODY_BYTES:
                raise _RequestError(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Webhook body exceeds 1 MiB"
                )
            content_type = self.headers.get("Content-Type", "")
            media, separator, parameter = content_type.partition(";")
            if media.strip().casefold() != "application/json" or (
                separator and parameter.strip().casefold() != "charset=utf-8"
            ):
                raise _RequestError(
                    HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Content-Type must be application/json"
                )
            event = self.headers.get("X-GitHub-Event")
            delivery = self.headers.get("X-GitHub-Delivery")
            signature = self.headers.get("X-Hub-Signature-256")
            if event is None or delivery is None or signature is None:
                raise _RequestError(
                    HTTPStatus.BAD_REQUEST, "Required GitHub delivery headers are missing"
                )
            return event, delivery, signature, length

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            try:
                if self.path != "/webhooks/github":
                    raise _RequestError(HTTPStatus.NOT_FOUND, "Unknown webhook endpoint")
                event, delivery, signature, length = self._headers()
                try:
                    body = self.rfile.read(length)
                except (OSError, TimeoutError) as exc:
                    raise _RequestError(
                        HTTPStatus.REQUEST_TIMEOUT, "Timed out reading webhook body"
                    ) from exc
                if len(body) != length:
                    raise _RequestError(HTTPStatus.BAD_REQUEST, "Webhook body ended early")
                expected = "sha256=" + hmac.new(token, body, hashlib.sha256).hexdigest()
                if _SIGNATURE.fullmatch(signature) is None or not hmac.compare_digest(
                    expected, signature
                ):
                    raise _RequestError(HTTPStatus.FORBIDDEN, "Webhook signature is invalid")
                result = ingestor.ingest(event, delivery, body)
                self._reply(HTTPStatus.ACCEPTED, result)
            except _RequestError as exc:
                self._reply(exc.status, {"error": exc.message})
            except UnknownRepository:
                self._reply(HTTPStatus.NOT_FOUND, {"error": "Repository is not registered"})
            except InvalidEvent:
                self._reply(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": "Invalid GitHub event"})
            except (EventConflict, EventRace):
                self._reply(HTTPStatus.CONFLICT, {"error": "Event state changed; retry delivery"})
            except EventError:
                self._reply(HTTPStatus.BAD_GATEWAY, {"error": "GitHub event lookup failed"})
            except Exception:
                self._reply(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Webhook ingestion failed"})

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            self._reply(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "POST is required"})

    return WebhookServer((host, port), Handler)
