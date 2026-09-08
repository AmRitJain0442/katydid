"""Exercise host supervision with a real exiting child and loopback listener."""

import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def test_host_restarts_exited_child_and_retains_bounded_logs(tmp_path):
    package = tmp_path / "katydid"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "__main__.py").write_text(
        "import os, sys\n"
        "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
        "class Handler(BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        if self.path == '/exit': os._exit(17)\n"
        "        body = str(os.getpid()).encode()\n"
        "        self.send_response(200)\n"
        "        self.send_header('Content-Length', str(len(body)))\n"
        "        self.end_headers()\n"
        "        self.wfile.write(body)\n"
        "print('test-worker-online', flush=True)\n"
        "port = int(sys.argv[sys.argv.index('--port')+1])\n"
        "HTTPServer(('127.0.0.1', port), Handler).serve_forever()\n"
    )
    fleet = tmp_path / "fleet.yaml"
    fleet.write_text("schema_version: 1\n")
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    logs = tmp_path / "logs"
    process = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "scripts/run_service.py"),
            "--fleet",
            str(fleet),
            "--logs",
            str(logs),
            "--port",
            str(port),
        ],
        env=dict(os.environ, PYTHONPATH=str(tmp_path)),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}),
    )

    def ready(previous=None):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            assert process.poll() is None
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=0.5) as response:
                    pid = int(response.read())
                    if pid != previous:
                        return pid
            except (OSError, urllib.error.URLError):
                pass
            time.sleep(0.1)
        raise AssertionError("Supervised worker did not become ready")

    try:
        original = ready()
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/exit", timeout=1).close()
        except OSError:
            pass
        assert ready(original) != original
        evidence = (logs / "service.log").read_text()
        assert "Restarting worker after 5 seconds" in evidence
        assert evidence.count("test-worker-online") == 2
        assert len(evidence) < 5_000_000
    finally:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                timeout=15,
                check=False,
            )
        else:
            process.terminate()
        process.wait(timeout=20)
