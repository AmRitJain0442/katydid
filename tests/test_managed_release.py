import json
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from katydid.deployment import LOG_FILE_BYTES, LOG_FILE_COUNT, _process_identity, _stop_owned


def git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repository,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=True,
    ).stdout.strip()


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def application(version: str, *, log_bytes: int = 0) -> str:
    return f"""import argparse
import json
import os
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = {version!r}

parser = argparse.ArgumentParser()
parser.add_argument("--host", required=True)
parser.add_argument("--port", required=True, type=int)
parser.add_argument("--db", required=True)
args = parser.parse_args()
revision = os.environ["KATYDID_RELEASE_COMMIT"]

for descriptor, byte in ((1, b"O"), (2, b"E")):
    remaining = {log_bytes}
    while remaining:
        chunk = byte * min(65536, remaining)
        os.write(descriptor, chunk)
        remaining -= len(chunk)

with sqlite3.connect(args.db) as connection:
    connection.execute("CREATE TABLE IF NOT EXISTS orders (value TEXT NOT NULL)")

class Server(ThreadingHTTPServer):
    allow_reuse_address = True

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Katydid-Revision", revision)
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/state":
            with sqlite3.connect(args.db) as connection:
                count = connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
            body = json.dumps({{"version": VERSION, "count": count}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def do_POST(self):
        if self.path != "/orders":
            self.send_error(404)
            return
        with sqlite3.connect(args.db) as connection:
            connection.execute("INSERT INTO orders VALUES ('preserved')")
        self.send_response(204)
        self.end_headers()

    def log_message(self, format, *args):
        pass

Server((args.host, args.port), Handler).serve_forever()
"""


def invoke(
    action: str,
    workspace: Path,
    commit: str,
    state: Path,
    spec: Path,
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "katydid.deployment",
            action,
            "--workspace",
            str(workspace),
            "--commit",
            commit,
            "--state",
            str(state),
            "--spec",
            str(spec),
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=45,
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout)
    return result


def invoke_recover(state: Path, spec: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "katydid.deployment",
            "recover",
            "--state",
            str(state.resolve()),
            "--spec",
            str(spec.resolve()),
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=45,
        check=True,
    )


def request(url: str, *, method: str = "GET") -> tuple[dict, str | None]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(urllib.request.Request(url, method=method), timeout=5) as response:
        body = response.read()
        value = json.loads(body) if body else {}
        return value, response.headers.get("X-Katydid-Revision")


@pytest.mark.parametrize("_iteration", range(3))
def test_real_revision_cutover_and_rollback_preserve_host_database(
    tmp_path: Path, _iteration: int
) -> None:
    repository = tmp_path / "service"
    repository.mkdir()
    git(repository, "init", "--initial-branch=main")
    git(repository, "config", "user.name", "Katydid Test")
    git(repository, "config", "user.email", "katydid@example.invalid")
    (repository / "app.py").write_text(application("v1"), encoding="utf-8")
    git(repository, "add", "app.py")
    git(repository, "commit", "-m", "version one")
    first = git(repository, "rev-parse", "HEAD")

    port = free_port()
    state = tmp_path / "managed-state"
    spec = tmp_path / "deployment.json"
    spec.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "argv": [
                    "{python}",
                    "app.py",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--db",
                    "{data}/orders.db",
                ],
                "working_directory": ".",
                "environment": {},
                "health": {
                    "url": f"http://127.0.0.1:{port}/health",
                    "expected_body": "ok",
                    "expected_headers": {"X-Katydid-Revision": "{commit}"},
                    "timeout_seconds": 10,
                    "interval_seconds": 0.05,
                    "request_timeout_seconds": 1,
                },
                "stop_timeout_seconds": 5,
            }
        ),
        encoding="utf-8",
    )

    active_operation = first
    started = False
    try:
        deployed = json.loads(invoke("deploy", repository, first, state, spec).stdout)
        started = True
        assert deployed == {
            "action": "deploy",
            "active_commit": first,
            "commit": first,
            "previous_commit": None,
            "reused": False,
        }
        registry = json.loads((state / "managed-release.json").read_text(encoding="utf-8"))
        token = registry["active"]["token"]
        ready_files = list((state / "launches").glob("*.ready.json"))
        assert len(ready_files) == 1
        supervisor_argv = json.loads(ready_files[0].read_text(encoding="utf-8"))["supervisor_argv"]
        assert all(token not in argument for argument in supervisor_argv)
        assert all(
            token not in path.name
            for directory in (state / "launches", state / "logs")
            for path in directory.iterdir()
        )

        _stop_owned(registry["active"], 5)
        recovered = json.loads(invoke_recover(state, spec).stdout)
        assert recovered == {
            "action": "recover",
            "active_commit": first,
            "recovered": True,
            "status": "healthy",
        }
        value, revision = request(f"http://127.0.0.1:{port}/state")
        assert value == {"version": "v1", "count": 0}
        assert revision is None
        request(f"http://127.0.0.1:{port}/orders", method="POST")

        (repository / "app.py").write_text(application("v2"), encoding="utf-8")
        git(repository, "add", "app.py")
        git(repository, "commit", "-m", "version two")
        second = git(repository, "rev-parse", "HEAD")
        active_operation = second

        deployed = json.loads(invoke("deploy", repository, second, state, spec).stdout)
        assert deployed["active_commit"] == second
        assert deployed["previous_commit"] == first
        assert (state / "releases" / first / "app.py").read_text(encoding="utf-8") == application(
            "v1"
        )
        assert (state / "releases" / second / "app.py").read_text(encoding="utf-8") == application(
            "v2"
        )
        value, _revision = request(f"http://127.0.0.1:{port}/state")
        assert value == {"version": "v2", "count": 1}
        assert (
            json.loads(invoke("health", repository, second, state, spec).stdout)["active_commit"]
            == second
        )

        restored = json.loads(invoke("rollback", repository, second, state, spec).stdout)
        assert restored == {"action": "rollback", "active_commit": first, "commit": second}
        assert json.loads(invoke("health", repository, second, state, spec).stdout) == {
            "action": "health",
            "active_commit": first,
            "commit": second,
        }
        value, _revision = request(f"http://127.0.0.1:{port}/state")
        assert value == {"version": "v1", "count": 1}
        with sqlite3.connect(state / "data" / "orders.db") as connection:
            assert connection.execute("SELECT value FROM orders").fetchall() == [("preserved",)]

        registry_path = state / "managed-release.json"
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        original_birth = registry["active"]["child"]["birth"]
        registry["active"]["child"]["birth"] = "wrong-birth-identity"
        registry_path.write_text(json.dumps(registry), encoding="utf-8")
        refused = invoke("stop", repository, second, state, spec, check=False)
        assert refused.returncode == 1
        assert "identity mismatch" in refused.stderr
        assert request(f"http://127.0.0.1:{port}/state")[0]["version"] == "v1"
        registry["active"]["child"]["birth"] = original_birth
        registry_path.write_text(json.dumps(registry), encoding="utf-8")

        stopped = json.loads(invoke("stop", repository, second, state, spec).stdout)
        started = False
        assert stopped["active_commit"] is None
        assert json.loads(invoke_recover(state, spec).stdout) == {
            "action": "recover",
            "active_commit": None,
            "recovered": False,
            "status": "skipped",
        }
    finally:
        if started:
            invoke("stop", repository, active_operation, state, spec, check=False)


def test_deploy_rejects_dirty_checkout_before_creating_state(tmp_path: Path) -> None:
    repository = tmp_path / "service"
    repository.mkdir()
    git(repository, "init", "--initial-branch=main")
    git(repository, "config", "user.name", "Katydid Test")
    git(repository, "config", "user.email", "katydid@example.invalid")
    (repository / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    git(repository, "add", "app.py")
    git(repository, "commit", "-m", "seed")
    commit = git(repository, "rev-parse", "HEAD")
    (repository / "ignored.tmp").write_text("artifact", encoding="utf-8")
    spec = tmp_path / "spec.json"
    spec.write_text("{}", encoding="utf-8")
    state = tmp_path / "state"

    result = invoke("deploy", repository, commit, state, spec, check=False)

    assert result.returncode == 1
    assert "including untracked and ignored files" in result.stderr
    assert not state.exists()


def test_supervisor_rotates_both_streams_across_crash_recovery(tmp_path: Path) -> None:
    repository = tmp_path / "noisy-service"
    repository.mkdir()
    git(repository, "init", "--initial-branch=main")
    git(repository, "config", "user.name", "Katydid Test")
    git(repository, "config", "user.email", "katydid@example.invalid")
    emitted = LOG_FILE_BYTES * LOG_FILE_COUNT + 123
    (repository / "app.py").write_text(application("noisy", log_bytes=emitted), encoding="utf-8")
    git(repository, "add", "app.py")
    git(repository, "commit", "-m", "noisy service")
    commit = git(repository, "rev-parse", "HEAD")

    port = free_port()
    state = tmp_path / "managed-state"
    spec = tmp_path / "deployment.json"
    spec.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "argv": [
                    "{python}",
                    "app.py",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--db",
                    "{data}/orders.db",
                ],
                "working_directory": ".",
                "environment": {},
                "health": {
                    "url": f"http://127.0.0.1:{port}/health",
                    "expected_headers": {"X-Katydid-Revision": "{commit}"},
                    "timeout_seconds": 15,
                    "interval_seconds": 0.05,
                    "request_timeout_seconds": 1,
                },
                "stop_timeout_seconds": 5,
            }
        ),
        encoding="utf-8",
    )

    started = False
    try:
        invoke("deploy", repository, commit, state, spec)
        started = True
        registry_path = state / "managed-release.json"
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        stdout = Path(registry["active"]["stdout"])
        stderr = Path(registry["active"]["stderr"])

        def assert_bounded(path: Path, expected: bytes) -> None:
            generations = [path, *(path.with_name(f"{path.name}.{i}") for i in (1, 2))]
            assert all(item.exists() for item in generations)
            assert all(item.stat().st_size <= LOG_FILE_BYTES for item in generations)
            assert LOG_FILE_BYTES * 2 < sum(item.stat().st_size for item in generations)
            assert sum(item.stat().st_size for item in generations) <= (
                LOG_FILE_BYTES * LOG_FILE_COUNT
            )
            assert all(set(item.read_bytes()) <= {expected[0]} for item in generations)

        assert stdout.name == f"{commit}.stdout.log"
        assert stderr.name == f"{commit}.stderr.log"
        assert_bounded(stdout, b"O")
        assert_bounded(stderr, b"E")

        _stop_owned(registry["active"], 5)
        assert json.loads(invoke_recover(state, spec).stdout)["recovered"] is True
        assert_bounded(stdout, b"O")
        assert_bounded(stderr, b"E")
        assert {path.name for path in (state / "logs").iterdir()} == {
            stdout.name,
            f"{stdout.name}.1",
            f"{stdout.name}.2",
            stderr.name,
            f"{stderr.name}.1",
            f"{stderr.name}.2",
        }
    finally:
        if started:
            invoke("stop", repository, commit, state, spec, check=False)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process identity regression")
def test_windows_process_identity_handles_exit_query_races() -> None:
    for _attempt in range(40):
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(0.02)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 2
        while _process_identity(process.pid) is not None and time.monotonic() < deadline:
            pass
        process.wait(timeout=2)
        assert _process_identity(process.pid) is None
