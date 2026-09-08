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
