"""Bounded loopback-only HTTP dashboard for Katydid task control.

The dashboard is intentionally a local control surface, not an authentication or
remote deployment boundary.  It uses fixed routes and a deliberately small HTTP
contract so callers can pair it with any durable task store.
"""

from __future__ import annotations

import ipaddress
import json
import re
import socket
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from urllib.parse import parse_qs, unquote, urlsplit

MAX_REQUEST_BYTES = 64 * 1024
MAX_INSTRUCTION_CHARS = 4_000
TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
ControlAction = Literal["pause", "resume", "cancel", "steer"]
STATIC_ROOT = Path(__file__).with_name("static")
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/static/app.css": ("app.css", "text/css; charset=utf-8"),
    "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/static/vultron-mark.svg": ("vultron-mark.svg", "image/svg+xml"),
}


class Store(Protocol):
    """Storage operations consumed by the dashboard."""

    def get_task(self, task_id: str) -> dict[str, Any]: ...

    def list_tasks(self) -> list[dict[str, Any]]: ...

    def events(self, task_id: str) -> list[dict[str, Any]]: ...

    def control(
        self,
        task_id: str,
        action: ControlAction,
        instruction: str | None = None,
    ) -> dict[str, Any]: ...


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        store: Store,
        repositories: tuple[str, ...],
        enqueue: Callable[[str], dict[str, Any]],
        runtime: Callable[[], dict[str, Any]] | None = None,
        live: Callable[[dict[str, Any], list[str]], dict[str, Any]] | None = None,
        reporting: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        self.store = store
        self.repositories = repositories
        self.enqueue = enqueue
        self.runtime = runtime
        self.live = live
        self.reporting = reporting
        try:
            if ipaddress.ip_address(address[0]).version == 6:
                self.address_family = socket.AF_INET6
        except ValueError:
            pass
        super().__init__(address, DashboardHandler)


def _loopback_hostname(hostname: str | None) -> bool:
    if hostname is None:
        return False
    name = hostname.rstrip(".").lower()
    if name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def _parse_authority(value: str) -> tuple[str, int | None] | None:
    if not value or value != value.strip() or any(char in value for char in "/\\,@"):
        return None
    try:
        parsed = urlsplit(f"//{value}")
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if not _loopback_hostname(hostname) or parsed.username is not None:
        return None
    return cast(str, hostname).rstrip(".").lower(), port


class DashboardHandler(BaseHTTPRequestHandler):
    """Fixed-route request handler with browser and body boundary checks."""

    server: DashboardServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        # The embedding CLI owns logging; avoid leaking task IDs to stderr by default.
        return

    def _send_bytes(
        self,
        status: HTTPStatus,
        body: bytes,
        content_type: str,
        *,
        head_only: bool = False,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; base-uri 'none'; "
            "form-action 'self'; frame-ancestors 'none'",
        )
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _send_json(self, status: HTTPStatus, value: object) -> None:
        try:
            body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError):
            status = HTTPStatus.INTERNAL_SERVER_ERROR
            body = b'{"error":"Store returned data that is not JSON serializable"}'
        self._send_bytes(status, body, "application/json; charset=utf-8")

    def _error(self, status: HTTPStatus, message: str) -> None:
        self._send_json(status, {"error": message})

    def _request_origin_is_safe(self, *, mutation: bool) -> bool:
        hosts = self.headers.get_all("Host", [])
        if len(hosts) != 1:
            self._error(HTTPStatus.BAD_REQUEST, "Exactly one Host header is required")
            return False
        host = _parse_authority(hosts[0])
        if host is None:
            self._error(HTTPStatus.FORBIDDEN, "Host must name the local machine")
            return False

        origins = self.headers.get_all("Origin", [])
        if len(origins) > 1:
            self._error(HTTPStatus.FORBIDDEN, "Multiple Origin headers are not accepted")
            return False
        if not origins:
            return True
        try:
            origin = urlsplit(origins[0])
            origin_port = origin.port
        except ValueError:
            origin = urlsplit("")
            origin_port = None
        if (
            origin.scheme != "http"
            or not _loopback_hostname(origin.hostname)
            or origin.username is not None
            or origin.path not in ("", "/")
            or origin.query
            or origin.fragment
        ):
            self._error(HTTPStatus.FORBIDDEN, "Origin must be a local HTTP origin")
            return False
        if mutation:
            origin_host = cast(str, origin.hostname).rstrip(".").lower()
            expected_port = host[1] if host[1] is not None else 80
            if origin_host != host[0] or (origin_port or 80) != expected_port:
                self._error(HTTPStatus.FORBIDDEN, "Origin does not match Host")
                return False
        return True

    def _read_json(self) -> dict[str, Any] | None:
        request_markers = self.headers.get_all("X-Katydid-Request", [])
        if request_markers != ["dashboard"]:
            self._error(HTTPStatus.FORBIDDEN, "Missing dashboard request header")
            return None
        content_types = self.headers.get_all("Content-Type", [])
        if len(content_types) != 1 or self.headers.get_content_type() != "application/json":
            self._error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Content-Type must be application/json")
            return None
        if self.headers.get_all("Transfer-Encoding", []):
            self.close_connection = True
            self._error(HTTPStatus.BAD_REQUEST, "Transfer-Encoding is not accepted")
            return None
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1:
            self._error(HTTPStatus.LENGTH_REQUIRED, "Exactly one Content-Length is required")
            return None
        try:
            length = int(lengths[0], 10)
        except ValueError:
            self._error(HTTPStatus.BAD_REQUEST, "Content-Length is invalid")
            return None
        if length < 0:
            self._error(HTTPStatus.BAD_REQUEST, "Content-Length is invalid")
            return None
        if length > MAX_REQUEST_BYTES:
            self.close_connection = True
            self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Request body exceeds 64 KiB")
            return None
        try:
            raw = self.rfile.read(length)
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._error(HTTPStatus.BAD_REQUEST, "Request body must be valid UTF-8 JSON")
            return None
        if not isinstance(value, dict):
            self._error(HTTPStatus.BAD_REQUEST, "Request body must be a JSON object")
            return None
        return value

    @staticmethod
    def _task_route(path: str) -> tuple[str, str] | None:
        pieces = path.split("/")
        if len(pieces) not in (4, 5) or pieces[:3] != ["", "api", "tasks"]:
            return None
        try:
            task_id = unquote(pieces[3], errors="strict")
        except UnicodeDecodeError:
            return None
        if not TASK_ID.fullmatch(task_id):
            return None
        suffix = pieces[4] if len(pieces) == 5 else ""
        return task_id, suffix

    def do_HEAD(self) -> None:
        if not self._request_origin_is_safe(mutation=False):
            return
        path = urlsplit(self.path).path
        static = STATIC_FILES.get(path)
        if static is None:
            self._error(HTTPStatus.NOT_FOUND, "Route not found")
            return
        filename, content_type = static
        try:
            body = (STATIC_ROOT / filename).read_bytes()
        except OSError:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "Dashboard asset is unavailable")
            return
        self._send_bytes(HTTPStatus.OK, body, content_type, head_only=True)

    def do_GET(self) -> None:
        if not self._request_origin_is_safe(mutation=False):
            return
        path = urlsplit(self.path).path
        static = STATIC_FILES.get(path)
        if static is not None:
            filename, content_type = static
            try:
                body = (STATIC_ROOT / filename).read_bytes()
            except OSError:
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "Dashboard asset is unavailable")
                return
            self._send_bytes(HTTPStatus.OK, body, content_type)
            return
        try:
            if path == "/api/runtime":
                value = self.server.runtime() if self.server.runtime else {"managed": False}
                self._send_json(HTTPStatus.OK, {"runtime": value})
                return
            if path == "/api/repositories":
                self._send_json(HTTPStatus.OK, {"repositories": self.server.repositories})
                return
            if path == "/api/tasks":
                self._send_json(HTTPStatus.OK, {"tasks": self.server.store.list_tasks()})
                return
            task_route = self._task_route(path)
            if task_route is not None:
                task_id, suffix = task_route
                if suffix == "":
                    self._send_json(HTTPStatus.OK, {"task": self.server.store.get_task(task_id)})
                    return
                if suffix == "events":
                    self._send_json(
                        HTTPStatus.OK,
                        {"events": self.server.store.events(task_id)},
                    )
                    return
                if suffix == "live":
                    task = self.server.store.get_task(task_id)
                    query = parse_qs(urlsplit(self.path).query, max_num_fields=8)
                    if set(query) - {"log"}:
                        raise ValueError("Only log selection is supported")
                    value = (
                        self.server.live(task, query.get("log", []))
                        if self.server.live
                        else {"available": False, "runs": [], "logs": {}}
                    )
                    self._send_json(HTTPStatus.OK, {"workflow": value})
                    return
                if suffix == "report":
                    task = self.server.store.get_task(task_id)
                    report = (
                        self.server.reporting(task) if self.server.reporting else {"enabled": False}
                    )
                    self._send_json(HTTPStatus.OK, {"report": report})
                    return
        except KeyError:
            self._error(HTTPStatus.NOT_FOUND, "Task not found")
            return
        except ValueError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        except Exception:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "Task store operation failed")
            return
        self._error(HTTPStatus.NOT_FOUND, "Route not found")

    def do_POST(self) -> None:
        if not self._request_origin_is_safe(mutation=True):
            return
        path = urlsplit(self.path).path
        if path != "/api/tasks":
            task_route = self._task_route(path)
            if task_route is None or task_route[1] != "control":
                self._error(HTTPStatus.NOT_FOUND, "Route not found")
                return
        body = self._read_json()
        if body is None:
            return
        try:
            if path == "/api/tasks":
                if set(body) != {"repository"} or not isinstance(body["repository"], str):
                    self._error(HTTPStatus.BAD_REQUEST, "Expected a repository string")
                    return
                repository = body["repository"]
                if repository not in self.server.repositories:
                    self._error(HTTPStatus.BAD_REQUEST, "Repository is not registered")
                    return
                self._send_json(HTTPStatus.CREATED, {"task": self.server.enqueue(repository)})
                return

            task_route = cast(tuple[str, str], self._task_route(path))
            task_id = task_route[0]
            allowed_keys = {"action", "instruction"}
            if not set(body).issubset(allowed_keys) or not isinstance(body.get("action"), str):
                self._error(HTTPStatus.BAD_REQUEST, "Expected an action and optional instruction")
                return
            raw_action = body["action"]
            if raw_action not in {"pause", "resume", "cancel", "steer"}:
                self._error(HTTPStatus.BAD_REQUEST, "Unknown control action")
                return
            action = cast(ControlAction, raw_action)
            instruction = body.get("instruction")
            if action == "steer":
                if (
                    not isinstance(instruction, str)
                    or not instruction.strip()
                    or len(instruction) > MAX_INSTRUCTION_CHARS
                ):
                    self._error(
                        HTTPStatus.BAD_REQUEST,
                        "Steer requires a non-empty instruction of at most 4000 characters",
                    )
                    return
                instruction = instruction.strip()
            elif instruction is not None:
                self._error(HTTPStatus.BAD_REQUEST, "Instruction is only valid for steer")
                return
            result = self.server.store.control(task_id, action, instruction)
            self._send_json(HTTPStatus.OK, {"task": result})
        except KeyError:
            self._error(HTTPStatus.NOT_FOUND, "Task not found")
        except ValueError as exc:
            self._error(HTTPStatus.CONFLICT, str(exc) or "Control action is not valid now")
        except Exception:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "Task operation failed")


def make_server(
    store: Store,
    repositories: list[str],
    enqueue: Callable[[str], dict[str, Any]],
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    runtime: Callable[[], dict[str, Any]] | None = None,
    live: Callable[[dict[str, Any], list[str]], dict[str, Any]] | None = None,
    reporting: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> ThreadingHTTPServer:
    """Create a loopback dashboard server; the caller owns serving and shutdown."""
    if not _loopback_hostname(host):
        raise ValueError("Dashboard host must be a loopback address")
    if not isinstance(port, int) or isinstance(port, bool) or not 0 <= port <= 65_535:
        raise ValueError("Dashboard port must be between 0 and 65535")
    if (
        not repositories
        or any(not isinstance(item, str) or not item.strip() for item in repositories)
        or len(set(repositories)) != len(repositories)
    ):
        raise ValueError("Repositories must be a non-empty list of unique names")
    return DashboardServer(
        (host, port), store, tuple(repositories), enqueue, runtime, live, reporting
    )
