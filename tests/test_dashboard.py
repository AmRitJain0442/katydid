import http.client
import json
import threading
from contextlib import contextmanager

import pytest

from katydid.dashboard import MAX_REQUEST_BYTES, make_server


class FakeStore:
    def __init__(self):
        self.tasks = {
            "task-1": {
                "id": "task-1",
                "repository": "demo <script>alert(1)</script>",
                "state": "running",
                "epoch": 2,
                "payload": {"finding": "unescaped <img src=x> evidence"},
            }
        }
        self.event_rows = {
            "task-1": [
                {
                    "type": "started",
                    "message": "worker <b>started</b>",
                    "timestamp": "2026-09-07T01:02:03Z",
                }
            ]
        }
        self.controls = []

    def get_task(self, task_id):
        return dict(self.tasks[task_id])

    def list_tasks(self):
        return [dict(task) for task in self.tasks.values()]

    def events(self, task_id):
        if task_id not in self.tasks:
            raise KeyError(task_id)
        return list(self.event_rows[task_id])

    def control(self, task_id, action, instruction=None):
        task = self.tasks[task_id]
        self.controls.append((task_id, action, instruction))
        task["state"] = {"pause": "paused", "resume": "running", "cancel": "cancelled"}.get(
            action, task["state"]
        )
        return dict(task)


@contextmanager
def running_dashboard():
    store = FakeStore()

    def enqueue(repository):
        task = {"id": "task-2", "repository": repository, "state": "queued", "epoch": 0}
        store.tasks[task["id"]] = task
        store.event_rows[task["id"]] = []
        return dict(task)

    server = make_server(store, ["demo", "sample"], enqueue, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield store, server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def request(port, method, path, body=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    request_headers = dict(headers or {})
    encoded = body
    if isinstance(body, (dict, list)):
        encoded = json.dumps(body).encode()
    connection.request(method, path, body=encoded, headers=request_headers)
    response = connection.getresponse()
    raw = response.read()
    result_headers = dict(response.getheaders())
    connection.close()
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        payload = raw
    return response.status, result_headers, payload


def mutation_headers(port, **extra):
    return {
        "Content-Type": "application/json",
        "X-Katydid-Request": "dashboard",
        "Origin": f"http://127.0.0.1:{port}",
        **extra,
    }


def test_dashboard_assets_are_local_and_hardened():
    with running_dashboard() as (_, port):
        status, headers, html = request(port, "GET", "/")
        assert status == 200
        assert b"Vultron" in html
        assert b"https://" not in html
        assert "default-src 'self'" in headers["Content-Security-Policy"]
        assert "Access-Control-Allow-Origin" not in headers

        status, _, script = request(port, "GET", "/static/app.js")
        assert status == 200
        assert b"textContent" in script
        assert b"innerHTML" not in script


def test_read_api_and_missing_tasks():
    with running_dashboard() as (_, port):
        status, _, payload = request(port, "GET", "/api/repositories")
        assert status == 200
        assert payload == {"repositories": ["demo", "sample"]}

        status, _, payload = request(port, "GET", "/api/tasks")
        assert status == 200
        assert payload["tasks"][0]["payload"]["finding"].startswith("unescaped")

        status, _, payload = request(port, "GET", "/api/tasks/task-1")
        assert status == 200
        assert payload["task"]["id"] == "task-1"

        status, _, payload = request(port, "GET", "/api/tasks/task-1/events")
        assert status == 200
        assert payload["events"][0]["type"] == "started"

        status, _, payload = request(port, "GET", "/api/tasks/missing")
        assert status == 404
        assert payload == {"error": "Task not found"}


def test_create_and_every_control_reach_callbacks():
    with running_dashboard() as (store, port):
        status, _, payload = request(
            port,
            "POST",
            "/api/tasks",
            {"repository": "sample"},
            mutation_headers(port),
        )
        assert status == 201
        assert payload["task"]["id"] == "task-2"

        for action in ("pause", "resume", "cancel"):
            status, _, payload = request(
                port,
                "POST",
                "/api/tasks/task-1/control",
                {"action": action},
                mutation_headers(port),
            )
            assert status == 200
            assert payload["task"]["id"] == "task-1"

        status, _, _ = request(
            port,
            "POST",
            "/api/tasks/task-1/control",
            {"action": "steer", "instruction": "  inspect the parser  "},
            mutation_headers(port),
        )
        assert status == 200
        assert store.controls == [
            ("task-1", "pause", None),
            ("task-1", "resume", None),
            ("task-1", "cancel", None),
            ("task-1", "steer", "inspect the parser"),
        ]


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"repository": "not-registered"}, 400),
        ({"repository": "demo", "extra": True}, 400),
        ([], 400),
        (b"not-json", 400),
    ],
)
def test_create_rejects_invalid_bodies(body, expected):
    with running_dashboard() as (_, port):
        status, _, _ = request(port, "POST", "/api/tasks", body, mutation_headers(port))
        assert status == expected


@pytest.mark.parametrize(
    "body",
    [
        {"action": "launch"},
        {"action": "steer"},
        {"action": "steer", "instruction": "   "},
        {"action": "pause", "instruction": "surprise"},
        {"action": "resume", "extra": 1},
    ],
)
def test_control_rejects_invalid_actions(body):
    with running_dashboard() as (_, port):
        status, _, _ = request(
            port,
            "POST",
            "/api/tasks/task-1/control",
            body,
            mutation_headers(port),
        )
        assert status == 400


def test_mutations_require_json_and_custom_header():
    with running_dashboard() as (_, port):
        status, _, _ = request(
            port,
            "POST",
            "/api/tasks",
            json.dumps({"repository": "demo"}),
            {"Content-Type": "text/plain"},
        )
        assert status == 403

        status, _, _ = request(
            port,
            "POST",
            "/api/tasks",
            json.dumps({"repository": "demo"}),
            {"X-Katydid-Request": "dashboard", "Content-Type": "text/plain"},
        )
        assert status == 415


def test_host_and_origin_checks_block_browser_rebinding():
    with running_dashboard() as (_, port):
        status, _, _ = request(port, "GET", "/api/tasks", headers={"Host": "evil.example"})
        assert status == 403

        status, _, _ = request(
            port, "GET", "/api/tasks", headers={"Origin": "https://evil.example"}
        )
        assert status == 403

        headers = mutation_headers(port)
        headers["Origin"] = "http://localhost:1"
        status, _, _ = request(port, "POST", "/api/tasks", {"repository": "demo"}, headers)
        assert status == 403


def test_body_limit_and_path_traversal_are_rejected():
    with running_dashboard() as (store, port):
        headers = mutation_headers(port, **{"Content-Length": str(MAX_REQUEST_BYTES + 1)})
        status, _, _ = request(port, "POST", "/api/tasks", headers=headers)
        assert status == 413
        assert set(store.tasks) == {"task-1"}

        for path in ("/static/../dashboard.py", "/static/%2e%2e/dashboard.py", "/api/tasks/a%2Fb"):
            status, _, _ = request(port, "GET", path)
            assert status == 404


def test_make_server_rejects_external_bindings_and_bad_registry():
    store = FakeStore()
    with pytest.raises(ValueError, match="loopback"):
        make_server(store, ["demo"], lambda repository: {}, host="0.0.0.0", port=0)
    with pytest.raises(ValueError, match="Repositories"):
        make_server(store, [], lambda repository: {}, port=0)
