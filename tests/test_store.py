import threading
import time

import pytest

from katydid.store import InvalidTransition, LeaseConflict, StaleLease, Store, TaskNotFound


def make_store(tmp_path):
    return Store(tmp_path / "state.db")


def test_tasks_are_durable_and_idempotent(tmp_path):
    store = make_store(tmp_path)
    task = store.create_task("org/repo", {"revision": "abc"}, "request-1")

    reopened = Store(tmp_path / "state.db")
    assert reopened.get_task(task["id"]) == task
    assert reopened.create_task("org/repo", {"revision": "abc"}, "request-1") == task
    assert len(reopened.list_tasks()) == 1
    assert [event["kind"] for event in reopened.events(task["id"])] == ["created"]

    with pytest.raises(ValueError, match="different task"):
        reopened.create_task("org/repo", {"revision": "changed"}, "request-1")
    with pytest.raises(ValueError, match="different task"):
        reopened.create_task("another/repo", {"revision": "abc"}, "request-1")


def test_exclusive_claim_transition_and_terminal_fencing(tmp_path):
    store = make_store(tmp_path)
    task = store.create_task("org/repo", {})
    lease = store.claim(task["id"], "worker-a")

    with pytest.raises(LeaseConflict):
        store.claim(task["id"], "worker-b")
    store.assert_active(lease)
    assert store.transition(lease, "executing", {"step": 1})["state"] == "executing"
    completed = store.transition(lease, "completed", {"evidence": "sha256:abc"})
    assert completed["state"] == "completed"
    assert completed["lease"] is None
    with pytest.raises(StaleLease):
        store.transition(lease, "failed")
    with pytest.raises(LeaseConflict):
        store.claim(task["id"], "worker-b")
    with pytest.raises(InvalidTransition):
        store.control(task["id"], "cancel")


def test_pause_resume_cancel_and_steer_revoke_leases(tmp_path):
    store = make_store(tmp_path)
    paused_task = store.create_task("org/paused", {"scope": "tests"})
    old_lease = store.claim(paused_task["id"], "worker-a")
    store.transition(old_lease, "planning")

    paused = store.control(paused_task["id"], "pause")
    assert paused["state"] == "paused"
    assert paused["lease"] is None
    with pytest.raises(StaleLease):
        store.assert_active(old_lease)
    with pytest.raises(LeaseConflict):
        store.claim(paused_task["id"], "worker-b")
    assert store.recover_expired() == []
    assert store.get_task(paused_task["id"])["state"] == "paused"

    resumed = store.control(paused_task["id"], "resume")
    assert resumed["state"] == "queued"
    new_lease = store.claim(paused_task["id"], "worker-b")
    steered = store.control(paused_task["id"], "steer", "focus on the API regression")
    assert steered["state"] == "queued"
    assert steered["payload"] == {"scope": "tests"}
    assert steered["instructions"] == ["focus on the API regression"]
    with pytest.raises(StaleLease):
        store.assert_active(new_lease)

    cancel_lease = store.claim(paused_task["id"], "worker-c")
    cancelled = store.control(paused_task["id"], "cancel")
    assert cancelled["state"] == "cancelled"
    with pytest.raises(StaleLease):
        store.transition(cancel_lease, "completed")


def test_release_queues_work_and_fences_old_lease(tmp_path):
    store = make_store(tmp_path)
    task = store.create_task("org/repo", {})
    first = store.claim(task["id"], "worker-a")
    store.transition(first, "verifying")
    store.release(first)

    assert store.get_task(task["id"])["state"] == "queued"
    with pytest.raises(StaleLease):
        store.assert_active(first)
    second = store.claim(task["id"], "worker-a")
    assert second.epoch > first.epoch


def test_expiry_recovery_requeues_without_allowing_false_completion(tmp_path):
    store = make_store(tmp_path)
    task = store.create_task("org/repo", {})
    expired = store.claim(task["id"], "worker-a", ttl_seconds=1)
    store.transition(expired, "merging")
    time.sleep(1.05)

    assert store.recover_expired() == [task["id"]]
    recovered = store.get_task(task["id"])
    assert recovered["state"] == "queued"
    assert recovered["lease"] is None
    with pytest.raises(StaleLease):
        store.transition(expired, "completed")
    replacement = store.claim(task["id"], "worker-b")
    assert replacement.epoch > expired.epoch


@pytest.mark.parametrize("state", ["publishing", "deploying", "monitoring"])
def test_expiry_during_external_effect_requires_reconciliation(tmp_path, state):
    store = make_store(tmp_path)
    task = store.create_task("org/repo", {})
    lease = store.claim(task["id"], "worker-a", ttl_seconds=1)
    store.transition(lease, state)
    time.sleep(1.05)

    assert store.recover_expired() == [task["id"]]
    recovered = store.get_task(task["id"])
    assert recovered["state"] == "unresolved"
    assert recovered["result"]["reconciliation_required"] is True
    with pytest.raises(StaleLease):
        store.transition(lease, "completed")


def test_renewal_keeps_epoch_and_extends_authority(tmp_path):
    store = make_store(tmp_path)
    task = store.create_task("org/repo", {})
    lease = store.claim(task["id"], "worker-a", ttl_seconds=1)
    renewed = store.renew(lease, ttl_seconds=10)

    assert renewed.epoch == lease.epoch
    assert renewed.expires_at > lease.expires_at
    store.assert_active(renewed)


def test_simultaneous_claims_have_one_winner(tmp_path):
    path = tmp_path / "state.db"
    task = Store(path).create_task("org/repo", {})
    barrier = threading.Barrier(3)
    leases = []
    failures = []

    def attempt(worker):
        candidate = Store(path)
        barrier.wait()
        try:
            leases.append(candidate.claim(task["id"], worker))
        except LeaseConflict as exc:
            failures.append(exc)

    threads = [threading.Thread(target=attempt, args=(worker,)) for worker in ("a", "b")]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert len(leases) == 1
    assert len(failures) == 1


def test_simultaneous_tasks_for_one_repository_have_one_winner(tmp_path):
    path = tmp_path / "state.db"
    store = Store(path)
    tasks = [store.create_task("org/shared", {"number": number}) for number in (1, 2)]
    barrier = threading.Barrier(3)
    leases = []
    failures = []

    def attempt(task, worker):
        candidate = Store(path)
        barrier.wait()
        try:
            leases.append(candidate.claim(task["id"], worker))
        except LeaseConflict as exc:
            failures.append(exc)

    threads = [
        threading.Thread(target=attempt, args=(task, f"worker-{index}"))
        for index, task in enumerate(tasks)
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert len(leases) == 1
    assert len(failures) == 1
    store.release(leases[0])
    remaining = next(task for task in tasks if task["id"] != leases[0].task_id)
    store.claim(remaining["id"], "later-worker")


def test_invalid_inputs_and_missing_tasks_fail_closed(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(ValueError):
        store.create_task("", {})
    with pytest.raises(ValueError):
        store.create_task("org/repo", {"bad": float("nan")})
    with pytest.raises(TaskNotFound):
        store.get_task("missing")

    task = store.create_task("org/repo", {})
    with pytest.raises(ValueError):
        store.control(task["id"], "steer")
    with pytest.raises(InvalidTransition):
        store.control(task["id"], "resume")
    lease = store.claim(task["id"], "worker")
    with pytest.raises(InvalidTransition):
        store.transition(lease, "queued")


def test_event_log_records_ordered_control_and_state_evidence(tmp_path):
    store = make_store(tmp_path)
    task = store.create_task("org/repo", {})
    lease = store.claim(task["id"], "worker")
    store.transition(lease, "executing", {"command": ["pytest"]})
    store.control(task["id"], "pause")

    events = store.events(task["id"])
    assert [event["kind"] for event in events] == ["created", "claimed", "transition", "pause"]
    assert events[2]["details"] == {"command": ["pytest"]}
    assert events[-1]["epoch"] > events[1]["epoch"]
    assert [event["id"] for event in events] == sorted(event["id"] for event in events)


def test_integration_pipeline_states_and_terminal_result(tmp_path):
    store = make_store(tmp_path)
    task = store.create_task("org/repo", {})
    lease = store.claim(task["id"], "worker")
    states = [
        "preparing",
        "testing",
        "diagnosing",
        "repairing",
        "verifying",
        "reviewing",
        "repairing",
        "verifying",
        "reviewing",
        "publishing",
        "deploying",
        "monitoring",
    ]
    for state in states:
        store.transition(lease, state)
    completed = store.transition(lease, "completed", {"artifact": "sha256:abc"})
    assert completed["result"] == {"artifact": "sha256:abc"}


@pytest.mark.parametrize("stage", ["publishing", "deploying", "monitoring"])
@pytest.mark.parametrize("action", ["pause", "cancel", "steer"])
def test_control_during_external_effect_requires_reconciliation(tmp_path, stage, action):
    store = make_store(tmp_path)
    task = store.create_task("org/repo", {})
    lease = store.claim(task["id"], "worker")
    store.transition(lease, stage)

    controlled = store.control(
        task["id"], action, "use the reconciled artifact" if action == "steer" else None
    )
    assert controlled["state"] == "unresolved"
    assert controlled["result"]["reconciliation_required"] is True
    assert controlled["result"]["control"] == action
    assert controlled["lease"] is None
    with pytest.raises(StaleLease):
        store.transition(lease, "completed")
    with pytest.raises(InvalidTransition):
        store.control(task["id"], "resume")
    with pytest.raises(InvalidTransition):
        store.control(task["id"], "steer", "retry")
    assert store.events(task["id"])[-1]["details"]["reconciliation_required"] is True
