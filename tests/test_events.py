import hashlib
import json
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from katydid.events import (
    EventRace,
    GitHubIngress,
    InvalidEvent,
    UnknownRepository,
    _gh_pull_request,
)
from katydid.store import EventConflict, EventNotFound, Store

BASE = "1" * 40
HEAD_ONE = "2" * 40
HEAD_TWO = "3" * 40


def controller(
    tmp_path: Path,
    *,
    pull_requests: bool = True,
    pushes: bool = True,
    releases: bool = False,
    isolated: bool = True,
) -> Any:
    events = SimpleNamespace(
        github_repository="acme/widgets",
        pull_requests=pull_requests,
        pushes=pushes,
        releases=releases,
    )
    repository = SimpleNamespace(
        id="widgets",
        source="https://github.com/acme/widgets",
        base_branch="main",
        events=events,
        isolation_policy=SimpleNamespace(required=True) if isolated else None,
        release=object() if releases else None,
    )
    return SimpleNamespace(
        config=SimpleNamespace(repositories=[repository]),
        digest="a" * 64,
        store=Store(tmp_path / "state.db"),
    )


def encoded(value: dict[str, Any]) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode()


def repository_payload(**extra: Any) -> dict[str, Any]:
    return {"repository": {"full_name": "acme/widgets"}, **extra}


def pull_payload(
    action: str = "synchronize",
    number: int = 7,
    base: str = "main",
    head: str = HEAD_ONE,
) -> bytes:
    return encoded(
        repository_payload(
            action=action,
            number=number,
            pull_request={
                "number": number,
                "head": {"sha": head, "ref": "feature"},
                "base": {"ref": base},
            },
        )
    )


def current_pull(state: str, head: str = HEAD_ONE) -> dict[str, Any]:
    return {
        "number": 7,
        "state": state,
        "head": {"sha": head, "ref": "feature"},
        "base": {
            "sha": BASE,
            "ref": "main",
            "repo": {"full_name": "acme/widgets"},
        },
    }


def test_pull_request_reconciles_current_state_deduplicates_and_closes(tmp_path: Path) -> None:
    control = controller(tmp_path)
    github_state = [current_pull("open", HEAD_ONE)]
    lookups: list[int] = []

    def pull(_repository: str, number: int) -> dict[str, Any]:
        lookups.append(number)
        return github_state[0]

    def revision(_source: str, ref: str) -> str:
        return (
            HEAD_TWO
            if ref == "refs/pull/7/head" and github_state[0]["head"]["sha"] == HEAD_TWO
            else (HEAD_ONE if ref == "refs/pull/7/head" else BASE)
        )

    ingress = GitHubIngress(control, pull_request=pull, revision=revision)
    first_body = pull_payload()
    first = ingress.ingest("pull_request", "delivery-pr-1", first_body)

    assert first["status"] == "enqueued"
    first_task = first["task"]
    assert first_task["payload"] == {
        "config_sha256": "a" * 64,
        "base_sha": BASE,
        "source_sha": HEAD_ONE,
        "source_ref": "refs/pull/7/head",
        "stage": "pull-request",
        "mode": "check",
        "event": {
            "provider": "github",
            "delivery_id": "delivery-pr-1",
            "type": "pull_request",
            "repository": "acme/widgets",
            "action": "synchronize",
            "number": 7,
            "head_ref": "feature",
        },
    }

    # A delayed close callback reconciles the current open head instead of cancelling it.
    github_state[0] = current_pull("open", HEAD_TWO)
    second = ingress.ingest("pull_request", "delivery-pr-2", pull_payload("closed", head=HEAD_TWO))
    assert second["status"] == "enqueued"
    assert second["task"]["payload"]["source_sha"] == HEAD_TWO
    assert control.store.get_task(first_task["id"])["state"] == "cancelled"

    # A later callback observes the PR closed and atomically tombstones current group work.
    github_state[0] = current_pull("closed", HEAD_TWO)
    third_body = pull_payload("synchronize", head=HEAD_TWO)
    third = ingress.ingest("pull_request", "delivery-pr-3", third_body)
    assert third["status"] == "cancelled"
    assert third["task"] is None
    assert control.store.get_task(second["task_id"])["state"] == "cancelled"

    lookup_count = len(lookups)
    replay = ingress.ingest("pull_request", "delivery-pr-3", third_body)
    assert replay["duplicate"] is True
    assert replay["status"] == "cancelled"
    assert replay["task_id"] is None
    assert len(lookups) == lookup_count
    with pytest.raises(EventConflict):
        ingress.ingest("pull_request", "delivery-pr-3", pull_payload("reopened"))
    assert len(lookups) == lookup_count


def test_pull_request_retries_group_cas_and_requires_isolation(tmp_path: Path) -> None:
    control = controller(tmp_path)
    original = control.store.ingest_event
    calls = 0
    lookups = 0

    def conflicting(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise EventConflict("simulated concurrent event")
        return original(*args, **kwargs)

    control.store.ingest_event = conflicting

    def pull(_repository: str, _number: int) -> dict[str, Any]:
        nonlocal lookups
        lookups += 1
        return current_pull("open")

    ingress = GitHubIngress(
        control,
        pull_request=pull,
        revision=lambda _source, ref: HEAD_ONE if ref.startswith("refs/pull/") else BASE,
    )
    result = ingress.ingest("pull_request", "delivery-cas", pull_payload())
    assert result["status"] == "enqueued"
    assert calls == 2
    assert lookups == 2

    unisolated = controller(tmp_path / "other", isolated=False)
    with pytest.raises(InvalidEvent, match="Docker isolation"):
        GitHubIngress(unisolated, pull_request=pull).ingest(
            "pull_request", "delivery-unisolated", pull_payload()
        )


def test_pull_request_retarget_tombstones_previous_base_work(tmp_path: Path) -> None:
    control = controller(tmp_path)
    github_state = [current_pull("open")]

    ingress = GitHubIngress(
        control,
        pull_request=lambda _repository, _number: github_state[0],
        revision=lambda _source, ref: HEAD_ONE if ref.startswith("refs/pull/") else BASE,
    )
    original = ingress.ingest("pull_request", "delivery-before-retarget", pull_payload("opened"))
    github_state[0] = {
        **current_pull("open"),
        "base": {
            "sha": BASE,
            "ref": "maintenance",
            "repo": {"full_name": "acme/widgets"},
        },
    }

    retargeted = ingress.ingest(
        "pull_request", "delivery-retarget", pull_payload("edited", base="maintenance")
    )

    assert retargeted["status"] == "cancelled"
    assert retargeted["task"] is None
    assert control.store.get_task(original["task_id"])["state"] == "cancelled"


def test_push_filters_and_supersedes_exact_current_base(tmp_path: Path) -> None:
    control = controller(tmp_path)
    current = [BASE]
    revisions: list[str] = []

    def revision(_source: str, ref: str) -> str:
        revisions.append(ref)
        return current[0]

    ingress = GitHubIngress(control, revision=revision)
    wrong = ingress.ingest(
        "push",
        "delivery-wrong-branch",
        encoded(repository_payload(ref="refs/heads/dev", after=BASE, deleted=False)),
    )
    deleted = ingress.ingest(
        "push",
        "delivery-deleted",
        encoded(repository_payload(ref="refs/heads/main", deleted=True)),
    )
    assert wrong["status"] == "ignored"
    assert deleted["status"] == "ignored"
    assert revisions == []

    first = ingress.ingest(
        "push",
        "delivery-push-1",
        encoded(repository_payload(ref="refs/heads/main", after=BASE, deleted=False)),
    )
    assert first["task"]["payload"]["stage"] == "merge"
    assert first["task"]["payload"]["mode"] == "check"

    current[0] = HEAD_ONE
    second = ingress.ingest(
        "push",
        "delivery-push-2",
        encoded(repository_payload(ref="refs/heads/main", after=HEAD_ONE, deleted=False)),
    )
    assert second["task"]["payload"]["source_ref"] == "refs/heads/main"
    assert control.store.get_task(first["task_id"])["state"] == "cancelled"

    with pytest.raises(EventRace):
        ingress.ingest(
            "push",
            "delivery-stale-push",
            encoded(repository_payload(ref="refs/heads/main", after=HEAD_TWO, deleted=False)),
        )
    with pytest.raises(EventNotFound):
        control.store.get_event("github", "delivery-stale-push")


def test_release_only_enqueues_a_published_tag_at_current_base(tmp_path: Path) -> None:
    control = controller(tmp_path, releases=True)
    tag_sha = [BASE]
    revisions: list[str] = []

    def revision(_source: str, ref: str) -> str:
        revisions.append(ref)
        return tag_sha[0] if ref.startswith("refs/tags/") else BASE

    ingress = GitHubIngress(control, revision=revision)
    body = encoded(repository_payload(action="published", release={"tag_name": "v1.2.3"}))
    accepted = ingress.ingest("release", "delivery-release-1", body)
    assert accepted["status"] == "enqueued"
    assert accepted["task"]["payload"]["stage"] == "release"
    assert accepted["task"]["payload"]["mode"] == "release"
    assert accepted["task"]["payload"]["source_ref"] == "refs/tags/v1.2.3"

    lease = control.store.claim(accepted["task_id"], "release-worker")
    control.store.transition(lease, "completed", {"release": "finished"})
    completed_events = control.store.events(accepted["task_id"])
    query_count = len(revisions)
    replay = ingress.ingest("release", "delivery-release-alias", body)
    assert replay["duplicate"] is True
    assert replay["delivery_id"] == "delivery-release-alias"
    assert replay["task_id"] == accepted["task_id"]
    assert replay["task"]["state"] == "completed"
    assert len(control.store.list_tasks()) == 1
    assert control.store.events(accepted["task_id"]) == completed_events
    assert len(revisions) == query_count
    with pytest.raises(EventConflict):
        ingress.ingest("push", "delivery-release-1", body)
    assert len(revisions) == query_count

    tag_sha[0] = HEAD_ONE
    old = ingress.ingest(
        "release",
        "delivery-release-old",
        encoded(repository_payload(action="published", release={"tag_name": "v1.0.0"})),
    )
    assert old["status"] == "ignored"
    assert old["task"] is None
    assert old["reason"] == "release tag is not the current base commit"


def test_concurrent_delivery_aliases_create_one_pull_request_task(tmp_path: Path) -> None:
    control = controller(tmp_path)
    query_barrier = threading.Barrier(2)

    def pull(_repository: str, _number: int) -> dict[str, Any]:
        query_barrier.wait(timeout=5)
        return current_pull("open")

    ingress = GitHubIngress(
        control,
        pull_request=pull,
        revision=lambda _source, ref: HEAD_ONE if ref.startswith("refs/pull/") else BASE,
    )
    body = pull_payload()
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(ingress.ingest, "pull_request", delivery, body)
            for delivery in ("delivery-race-1", "delivery-race-2")
        ]
        results = [future.result(timeout=10) for future in futures]

    assert results[0]["task_id"] == results[1]["task_id"]
    assert sorted(result["duplicate"] for result in results) == [False, True]
    assert len(control.store.list_tasks()) == 1
    assert control.store.group_version("widgets", "github:pull_request:7") == 1
    for delivery in ("delivery-race-1", "delivery-race-2"):
        assert control.store.get_event("github", delivery)["task_id"] == results[0]["task_id"]


def test_legacy_receipt_without_replay_key_never_creates_replacement_work(tmp_path: Path) -> None:
    control = controller(tmp_path)
    body = pull_payload()
    body_sha256 = hashlib.sha256(body).hexdigest()
    legacy = control.store.ingest_event(
        "github",
        "delivery-legacy",
        body_sha256,
        "widgets",
        {"legacy": True},
        group="github:pull_request:7",
        replay_key=None,
    )

    def no_lookup(_repository: str, _number: int) -> dict[str, Any]:
        raise AssertionError("legacy receipt replay must not query GitHub")

    replay = GitHubIngress(control, pull_request=no_lookup).ingest(
        "pull_request", "delivery-legacy", body
    )

    assert replay["duplicate"] is True
    assert replay["task_id"] == legacy["task_id"]
    assert len(control.store.list_tasks()) == 1


def test_invalid_and_disabled_events_are_bounded_before_provider_lookup(tmp_path: Path) -> None:
    control = controller(tmp_path, pull_requests=False)
    called = False

    def pull(_repository: str, _number: int) -> dict[str, Any]:
        nonlocal called
        called = True
        raise AssertionError("disabled and ignored events must not query GitHub")

    ingress = GitHubIngress(control, pull_request=pull)
    assert (
        ingress.ingest("pull_request", "delivery-disabled", pull_payload())["status"] == "ignored"
    )
    assert (
        ingress.ingest("pull_request", "delivery-action", pull_payload("labeled"))["status"]
        == "ignored"
    )
    assert (
        ingress.ingest(
            "ping", "delivery-ping", encoded(repository_payload(zen="Keep it logically awesome."))
        )["status"]
        == "ignored"
    )
    assert called is False

    with pytest.raises(InvalidEvent, match="Duplicate JSON"):
        ingress.ingest(
            "push",
            "delivery-duplicate-json",
            b'{"repository":{"full_name":"acme/widgets"},"ref":"a","ref":"b"}',
        )
    with pytest.raises(InvalidEvent, match="Non-finite"):
        ingress.ingest(
            "push",
            "delivery-nan",
            b'{"repository":{"full_name":"acme/widgets"},"value":NaN}',
        )
    with pytest.raises(InvalidEvent, match="1 MiB"):
        ingress.ingest("push", "delivery-large", b"{" + b" " * (1024 * 1024))
    with pytest.raises(UnknownRepository):
        ingress.ingest(
            "push",
            "delivery-unknown",
            encoded(
                {
                    "repository": {"full_name": "outside/unknown"},
                    "ref": "refs/heads/main",
                    "after": BASE,
                    "deleted": False,
                }
            ),
        )


def test_default_github_reader_uses_a_bounded_read_only_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[Any] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        observed.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout='{"number":7}', stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    assert _gh_pull_request("acme/widgets", 7) == {"number": 7}
    argv, kwargs = observed[0]
    assert argv == ["gh", "api", "--method", "GET", "repos/acme/widgets/pulls/7"]
    assert kwargs["shell"] is False
    assert kwargs["timeout"] == 30
