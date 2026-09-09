import json
import subprocess
import threading
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from katydid.fleet import RepositoryConfig
from katydid.reporting import (
    CommentReporter,
    GitHubComments,
    ReportingError,
    comment_body,
    target_for,
)
from katydid.store import Store

HEAD = "a" * 40
DIGEST = "b" * 64


class FakeGitHub(GitHubComments):
    def __init__(self):
        super().__init__()
        self.comments = []
        self.calls = []
        self.head = HEAD
        self.repository = "owner/orders"
        self.fail_after_create = False

    def request(self, path, method="GET", body=None):
        self.calls.append((path, method, body))
        if path == "user":
            return {"id": 42}
        if path == "repos/owner/orders/pulls/3":
            return {
                "number": 3,
                "base": {"ref": "main", "repo": {"full_name": self.repository}},
                "head": {"sha": self.head},
            }
        if "/comments?" in path:
            return self.comments.copy()
        if method == "POST":
            value = {"id": 100 + len(self.comments), "user": {"id": 42}, "body": body["body"]}
            self.comments.append(value)
            if self.fail_after_create:
                self.fail_after_create = False
                raise ReportingError("Response lost after create")
            return value
        if method == "PATCH":
            value = next(
                item for item in self.comments if item["id"] == int(path.rsplit("/", 1)[1])
            )
            value["body"] = body["body"]
            return value.copy()
        raise AssertionError((path, method))


def setup_reporter(tmp_path):
    repo = RepositoryConfig.model_validate(
        {
            "id": "orders",
            "source": "https://github.com/owner/orders.git",
            "profile": "quality.yaml",
            "context_paths": ["app.py"],
            "requirements": "Verify orders",
            "required_checks": {"unit": "test"},
            "github_comments": True,
        }
    )
    store = Store(tmp_path / "state.db")
    task = store.create_task(
        "orders",
        {
            "config_sha256": DIGEST,
            "source_ref": "refs/pull/3/head",
            "source_sha": HEAD,
            "base_sha": "c" * 40,
            "stage": "pull-request",
            "mode": "check",
            "event": {
                "provider": "github",
                "type": "pull_request",
                "repository": "owner/orders",
                "number": 3,
            },
        },
    )
    controller = SimpleNamespace(
        directory=tmp_path,
        digest=DIGEST,
        store=store,
        config=SimpleNamespace(repository=lambda _: repo),
        _unchanged_policy=lambda: None,
    )
    client = FakeGitHub()
    reporter = CommentReporter(controller, threading.Event(), client)
    return controller, reporter, client, task, repo


def due(reporter):
    with closing(reporter.connect()) as connection:
        connection.execute("UPDATE reports SET next_attempt=0")


def test_comment_creates_once_updates_in_place_and_does_not_repost_identical_body(tmp_path):
    controller, reporter, client, task, _ = setup_reporter(tmp_path)
    reporter.tick()
    assert reporter.status(task)["state"] == "posted"
    assert len(client.comments) == 1
    count = len(client.calls)
    reporter.tick()
    assert len(client.calls) == count
    lease = controller.store.claim(task["id"], "test-worker")
    controller.store.transition(lease, "testing")
    due(reporter)
    reporter.tick()
    assert len(client.comments) == 1
    assert "**Status:** testing" in client.comments[0]["body"]
    assert len([call for call in client.calls if call[1] == "PATCH"]) == 1
    assert reporter.status(task)["url"].endswith("/pull/3#issuecomment-100")


def test_lost_create_response_recovers_same_comment_after_reporter_restart(tmp_path):
    controller, reporter, client, task, _ = setup_reporter(tmp_path)
    client.fail_after_create = True
    reporter.tick()
    assert reporter.status(task)["state"] == "retrying"
    assert controller.store.get_task(task["id"])["state"] == "queued"
    assert len(client.comments) == 1
    restarted = CommentReporter(controller, threading.Event(), client)
    due(restarted)
    restarted.tick()
    assert restarted.status(task)["state"] == "posted"
    assert len(client.comments) == 1
    assert len([call for call in client.calls if call[1] == "POST"]) == 1


def test_comment_matching_checks_authorship_and_rejects_ambiguity(tmp_path):
    controller, _, client, task, repo = setup_reporter(tmp_path)
    target = target_for(repo, task, [])
    body = comment_body(task, target, {})
    client.comments = [{"id": 99, "user": {"id": 999}, "body": body}]
    client.upsert(target, body)
    assert client.comments[0]["id"] == 99
    assert len(client.comments) == 2
    client.comments.append({"id": 102, "user": {"id": 42}, "body": body})
    with pytest.raises(ReportingError, match="Multiple owned"):
        client.upsert(target, body)


def test_changed_pr_head_marks_existing_report_superseded_without_creating_new_one(tmp_path):
    _, reporter, client, task, repo = setup_reporter(tmp_path)
    reporter.tick()
    target = target_for(repo, task, [])
    client.head = "d" * 40
    result = client.upsert(target, comment_body(task, target, {}))
    assert result["state"] == "superseded"
    assert "**Superseded:**" in client.comments[0]["body"]
    client.comments.clear()
    result = client.upsert(target, comment_body(task, target, {}))
    assert result["state"] == "superseded"
    assert client.comments == []


def test_wrong_target_and_revoked_policy_never_post(tmp_path):
    controller, reporter, client, task, repo = setup_reporter(tmp_path)
    target = target_for(repo, task, [])
    client.repository = "another/repo"
    with pytest.raises(ReportingError, match="registered PR"):
        client.upsert(target, comment_body(task, target, {}))
    assert client.comments == []
    client.repository = "owner/orders"

    def revoked():
        raise ReportingError("Policy revoked")

    with pytest.raises(ReportingError, match="revoked"):
        client.upsert(target, comment_body(task, target, {}), revoked)
    assert client.comments == []
    reporter.collect()
    controller.config.repository = lambda _: repo.model_copy(update={"github_comments": False})
    reporter.tick()
    assert reporter.status(task)["state"] == "disabled"
    assert client.comments == []


def test_reporting_only_associates_verified_pr_events_or_controller_created_prs(tmp_path):
    _, _, _, task, repo = setup_reporter(tmp_path)
    assert target_for(repo, task, [])["number"] == 3
    assert target_for(repo.model_copy(update={"github_comments": False}), task, []) is None
    task["payload"]["event"]["repository"] = "foreign/repo"
    assert target_for(repo, task, []) is None
    task["payload"] = {"source_ref": "refs/heads/main"}
    assert target_for(repo, task, []) is None
    events = [
        {
            "kind": "transition",
            "details": {
                "candidate_sha": HEAD,
                "pull_request": {"number": 3, "url": "https://github.com/owner/orders/pull/3"},
            },
        }
    ]
    assert target_for(repo, task, events)["number"] == 3
    events[0]["details"]["pull_request"]["url"] = "https://github.com/foreign/repo/pull/3"
    assert target_for(repo, task, events) is None


def test_public_report_summarizes_tool_evidence_without_raw_output_or_credentials(tmp_path):
    _, _, _, task, repo = setup_reporter(tmp_path)
    task["result"] = {
        "diagnosis": {
            "summary": (
                "bad @everyone `x` | <script> from C:\\Users\\private\\file.py "
                "api_key=AIzaSensitive ghp_sensitive"
            )
        },
        "outcome": "failed",
    }
    workflow = {
        "runs": [{"steps": [{"name": "browser", "tool": "Playwright", "status": "running"}]}],
        "logs": {"secret": "PRIVATE RAW OUTPUT"},
    }
    body = comment_body(task, target_for(repo, task, []), workflow)
    assert "Playwright" in body
    for secret in (
        "@everyone",
        "<script>",
        "C:\\Users",
        "AIzaSensitive",
        "ghp_sensitive",
        "PRIVATE RAW OUTPUT",
    ):
        assert secret not in body
    assert "&#124;" in body
    assert "/pull/3/checks" in body


def test_github_comment_body_is_a_literal_json_file_not_shell_code(monkeypatch):
    text = "Literal `code` $(do-not-run)\nsecond line"
    observed = []

    def run(argv, **kwargs):
        assert kwargs["shell"] is False
        path = argv[argv.index("--input") + 1]
        observed.append(json.loads(Path(path).read_text(encoding="utf-8")))
        return subprocess.CompletedProcess(argv, 0, '{"ok": true}', "")

    monkeypatch.setattr(subprocess, "run", run)
    GitHubComments().request("repos/owner/orders/issues/3/comments", "POST", {"body": text})
    assert observed == [{"body": text}]


def test_comment_network_wait_does_not_hold_task_control_lock(tmp_path):
    controller, reporter, client, task, _ = setup_reporter(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = client.upsert

    def blocked(*args):
        entered.set()
        assert release.wait(10)
        return original(*args)

    client.upsert = blocked
    thread = threading.Thread(target=reporter.tick)
    thread.start()
    try:
        assert entered.wait(5)
        controller.store.control(task["id"], "cancel")
        assert controller.store.get_task(task["id"])["state"] == "cancelled"
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive()
