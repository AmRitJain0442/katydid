"""Opt-in signed PR delivery through the real controller and Docker executor."""

import hashlib
import hmac
import json
import threading
from urllib.request import Request, urlopen

import yaml
from test_controller import DeterministicTestDouble, git, make_controller, repository_policy
from test_docker_integration import (
    assert_containers_removed,
)
from test_docker_integration import (
    docker_image as docker_image,
)
from test_stages import staged_repository

from katydid.events import GitHubIngress
from katydid.webhook import make_webhook_server
from katydid.workspace import source_head


def test_signed_pr_delivery_tests_exact_head_in_docker_without_ai_or_delivery(
    tmp_path, docker_image
):
    repository = staged_repository(tmp_path, "service", healthy=False)
    profile_path = repository / "quality.yaml"
    profile = yaml.safe_load(profile_path.read_text())
    profile["isolation"] = {
        "adapter": "docker",
        "image": docker_image,
        "files": ["app.py", "verify.py"],
    }
    profile_path.write_text(yaml.safe_dump(profile))
    git(repository, "commit", "-am", "require isolated PR checks")
    base = source_head(str(repository), "main")
    git(repository, "switch", "-c", "feature")
    (repository / "app.py").write_text("VALUE = 2\n")
    git(repository, "commit", "-am", "fix PR candidate")
    head = git(repository, "rev-parse", "HEAD")
    git(repository, "update-ref", "refs/pull/7/head", head)
    git(repository, "switch", "main")
    policy = repository_policy(repository)
    policy["events"] = {"github_repository": "firm/service"}
    policy["isolation_policy"] = {"images": [docker_image], "files": ["app.py", "verify.py"]}
    provider = DeterministicTestDouble()
    controller = make_controller(tmp_path, [policy], provider)
    current = {
        "number": 7,
        "state": "open",
        "base": {"sha": base, "ref": "main", "repo": {"full_name": "firm/service"}},
        "head": {"sha": head, "ref": "feature"},
    }
    ingress = GitHubIngress(controller, pull_request=lambda _repo, _number: current)
    secret = b"fixture-webhook-secret-" + b"x" * 32
    server = make_webhook_server(ingress, secret, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    body = json.dumps(
        {
            "repository": {"full_name": "firm/service"},
            "action": "opened",
            "number": 7,
            "pull_request": {"number": 7, "base": {"ref": "main"}},
        }
    ).encode()
    signature = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()

    def send():
        request = Request(
            f"http://127.0.0.1:{server.server_port}/webhooks/github",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "signed-real-pr",
                "X-Hub-Signature-256": signature,
            },
        )
        with urlopen(request, timeout=30) as response:
            return json.load(response)

    try:
        received = send()
        task = received["task"]
        assert task["payload"]["source_sha"] == head
        result = controller.work_once()
        assert result["id"] == task["id"]
        assert result["state"] == "completed", result
        assert result["result"]["source_sha"] == head
        assert result["result"]["base_sha"] == base
        assert result["result"]["stage"] == "pull-request"
        assert result["result"]["mode"] == "check"
        isolation = result["result"]["baseline"]["isolation"]
        assert isolation["ready"] and isolation["cleanup_complete"]
        assert len(isolation["resources"]) == 2
        assert_containers_removed([item["id"] for item in isolation["resources"]])
        assert send()["duplicate"]
        assert len(controller.store.list_tasks()) == 1
        assert source_head(str(repository), "main") == base
        assert provider.roles == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
