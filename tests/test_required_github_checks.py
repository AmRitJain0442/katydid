import threading

import pytest

from katydid import workspace
from katydid.fleet import DeliveryConfig


def check(name, conclusion="SUCCESS"):
    return {"name": name, "status": "COMPLETED", "conclusion": conclusion}


def arrange(monkeypatch, checks):
    payload = {
        "headRefOid": "a" * 40,
        "state": "OPEN",
        "mergeStateStatus": "CLEAN",
        "statusCheckRollup": checks,
    }
    monkeypatch.setattr(workspace, "_gh_json", lambda *args: payload)
    monkeypatch.setattr(workspace, "NO_CHECK_GRACE_SECONDS", 0)
    clock = iter(range(100))
    monkeypatch.setattr(workspace.time, "monotonic", lambda: next(clock))
    return payload


@pytest.mark.parametrize(
    "checks",
    [[], [check("renamed")], [check("unit", "SKIPPED")], [check("unit", "NEUTRAL")]],
)
def test_missing_or_unexecuted_required_check_cannot_authorize_merge(monkeypatch, checks):
    arrange(monkeypatch, checks)
    with pytest.raises(workspace.WorkspaceError, match="Timed out"):
        workspace.wait_pull_request(
            "owner/repo", 1, "a" * 40, threading.Event(), 1, required_checks=("unit",)
        )


def test_all_named_check_runs_and_status_contexts_must_succeed(monkeypatch):
    expected = arrange(
        monkeypatch,
        [check("unit"), {"__typename": "StatusContext", "context": "browser", "state": "SUCCESS"}],
    )
    result = workspace.wait_pull_request(
        "owner/repo", 1, "a" * 40, threading.Event(), 1, required_checks=("unit", "browser")
    )
    assert result == expected


def test_duplicate_failed_check_cannot_be_hidden_by_passing_check(monkeypatch):
    arrange(monkeypatch, [check("unit"), check("unit", "FAILURE")])
    with pytest.raises(workspace.WorkspaceError, match="status check failed"):
        workspace.wait_pull_request(
            "owner/repo", 1, "a" * 40, threading.Event(), 1, required_checks=("unit",)
        )


@pytest.mark.parametrize("names", [[""], ["unit", "unit"], ["unit\n"], ["x" * 201]])
def test_delivery_rejects_invalid_required_check_names(names):
    with pytest.raises(ValueError):
        DeliveryConfig(mode="github", github_repository="owner/repo", github_required_checks=names)


def test_local_delivery_cannot_configure_github_checks():
    with pytest.raises(ValueError, match="require GitHub delivery"):
        DeliveryConfig(mode="local", github_required_checks=["unit"])


def api_payloads(monkeypatch, *, checks=None, statuses=None, response_sha="a" * 40):
    calls = []

    def request(timeout, *args):
        calls.append((timeout, args))
        if args[1].endswith("/check-runs"):
            runs = checks or []
            return {"total_count": len(runs), "check_runs": runs}
        contexts = statuses or []
        return {"sha": response_sha, "total_count": len(contexts), "statuses": contexts}

    monkeypatch.setattr(workspace, "_gh_json_timeout", request)
    return calls


def commit_check(name, *, state="success", sha="a" * 40):
    return {
        "name": name,
        "head_sha": sha,
        "status": "completed" if state != "pending" else "in_progress",
        "conclusion": state if state != "pending" else None,
    }


def test_exact_commit_check_runs_and_latest_status_contexts_must_succeed(monkeypatch):
    calls = api_payloads(
        monkeypatch,
        checks=[commit_check("unit")],
        statuses=[{"context": "security", "state": "success"}],
    )

    result = workspace.wait_commit_checks(
        "owner/repo", "a" * 40, threading.Event(), ("unit", "security"), 10
    )

    assert result == {
        "repository": "owner/repo",
        "sha": "a" * 40,
        "checks": [
            {"name": "unit", "kind": "check_run", "state": "success"},
            {"name": "security", "kind": "status_context", "state": "success"},
        ],
    }
    assert any("filter=latest" in args for _timeout, args in calls)
    assert all("per_page=100" in args for _timeout, args in calls)


@pytest.mark.parametrize(
    ("checks", "statuses"),
    [
        ([commit_check("unit", sha="b" * 40)], []),
        ([], [{"context": "unit", "state": "success"}]),
    ],
    ids=("check-run", "combined-status"),
)
def test_commit_api_response_must_match_exact_sha(monkeypatch, checks, statuses):
    api_payloads(
        monkeypatch,
        checks=checks,
        statuses=statuses,
        response_sha="b" * 40 if statuses else "a" * 40,
    )
    with pytest.raises(workspace.WorkspaceError, match="exact SHA|status data"):
        workspace.wait_commit_checks("owner/repo", "a" * 40, threading.Event(), ("unit",), 10)


def test_commit_check_missing_or_pending_waits_and_then_succeeds(monkeypatch):
    observations = iter(
        [
            [{"name": "unit", "kind": "check_run", "state": "pending"}],
            [{"name": "unit", "kind": "check_run", "state": "success"}],
        ]
    )
    monkeypatch.setattr(workspace, "_commit_check_observations", lambda *_args: next(observations))
    monkeypatch.setattr(workspace, "PULL_REQUEST_POLL_SECONDS", 0)

    result = workspace.wait_commit_checks("owner/repo", "a" * 40, threading.Event(), ("unit",), 10)

    assert result["checks"] == [{"name": "unit", "kind": "check_run", "state": "success"}]


@pytest.mark.parametrize(
    ("observations", "message"),
    [
        ([{"name": "unit", "kind": "check_run", "state": "failed"}], "failed"),
        (
            [
                {"name": "unit", "kind": "check_run", "state": "success"},
                {"name": "unit", "kind": "status_context", "state": "success"},
            ],
            "ambiguous",
        ),
    ],
)
def test_failed_or_ambiguous_commit_check_never_authorizes_release(
    monkeypatch, observations, message
):
    monkeypatch.setattr(workspace, "_commit_check_observations", lambda *_args: observations)
    with pytest.raises(workspace.WorkspaceError, match=message):
        workspace.wait_commit_checks("owner/repo", "a" * 40, threading.Event(), ("unit",), 10)


def test_missing_commit_check_times_out_and_cancellation_is_immediate(monkeypatch):
    monkeypatch.setattr(workspace, "_commit_check_observations", lambda *_args: [])
    clock = iter((0.0, 0.0, 2.0))
    monkeypatch.setattr(workspace.time, "monotonic", lambda: next(clock))
    with pytest.raises(workspace.WorkspaceError, match="Timed out"):
        workspace.wait_commit_checks("owner/repo", "a" * 40, threading.Event(), ("unit",), 1)

    monkeypatch.setattr(workspace.time, "monotonic", lambda: 0.0)
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(workspace.WorkspaceError, match="cancelled"):
        workspace.wait_commit_checks("owner/repo", "a" * 40, cancelled, ("unit",), 10)


def test_commit_check_pagination_is_bounded(monkeypatch):
    monkeypatch.setattr(
        workspace,
        "_gh_json_timeout",
        lambda *_args: {"total_count": 1001, "check_runs": []},
    )
    with pytest.raises(workspace.WorkspaceError, match="excessive"):
        workspace.wait_commit_checks("owner/repo", "a" * 40, threading.Event(), ("unit",), 10)
