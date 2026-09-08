import json
import sqlite3
import threading
from contextlib import contextmanager

import pytest

import katydid.store as store_module
from katydid.store import (
    EventConflict,
    EventNotFound,
    InvalidTransition,
    LeaseConflict,
    StaleLease,
    Store,
)

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
REPLAY_A = "c" * 64


def make_store(tmp_path):
    return Store(tmp_path / "state.db")


def monitoring_task(store, repository="service"):
    task = store.create_task(repository, {})
    lease = store.claim(task["id"], "release-worker")
    store.transition(lease, "monitoring")
    return task, lease


def test_release_success_pointer_requires_and_records_current_monitoring_authority(tmp_path):
    store = make_store(tmp_path)
    task, lease = monitoring_task(store)
    commit_sha = "1" * 40

    store.record_release_success(lease, commit_sha)

    pointer = tmp_path / "releases" / "service" / "last-success.json"
    assert json.loads(pointer.read_text(encoding="utf-8")) == {
        "commit": commit_sha,
        "task": task["id"],
    }
    event = store.events(task["id"])[-1]
    assert event["kind"] == "release_success"
    assert event["state"] == "monitoring"
    assert event["epoch"] == lease.epoch
    assert event["details"] == {"commit": commit_sha}


def test_pause_after_health_before_release_record_prevents_pointer_mutation(tmp_path):
    store = make_store(tmp_path)
    task, lease = monitoring_task(store)
    store.control(task["id"], "pause")

    with pytest.raises(StaleLease):
        store.record_release_success(lease, "2" * 40)

    assert not (tmp_path / "releases").exists()
    assert all(event["kind"] != "release_success" for event in store.events(task["id"]))


def test_release_lease_expiring_while_write_lock_waits_cannot_mutate_pointer(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    task = store.create_task("service", {})
    lease = store.claim(task["id"], "release-worker", ttl_seconds=1)
    store.transition(lease, "monitoring")
    entered = threading.Event()
    allow_lock = threading.Event()
    failures = []
    clock = [lease.expires_at - 0.1]
    original_write = store._write

    @contextmanager
    def delayed_write_lock():
        with original_write() as connection:
            entered.set()
            if not allow_lock.wait(5):
                raise AssertionError("test did not release delayed write lock")
            yield connection

    monkeypatch.setattr(store, "_write", delayed_write_lock)
    monkeypatch.setattr(store_module.time, "time", lambda: clock[0])

    def record():
        try:
            store.record_release_success(lease, "9" * 40)
        except BaseException as exc:
            failures.append(exc)

    writer = threading.Thread(target=record)
    writer.start()
    assert entered.wait(5)
    clock[0] = lease.expires_at + 0.1
    allow_lock.set()
    writer.join(5)

    assert not writer.is_alive()
    assert len(failures) == 1 and isinstance(failures[0], StaleLease)
    assert not (tmp_path / "releases").exists()
    assert all(event["kind"] != "release_success" for event in store.events(task["id"]))


def test_concurrent_control_waits_for_release_pointer_critical_section(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    task, lease = monitoring_task(store)
    entered = threading.Event()
    allow_replace = threading.Event()
    control_finished = threading.Event()
    failures = []
    original_replace = store_module.os.replace

    def blocked_replace(source, destination):
        entered.set()
        if not allow_replace.wait(5):
            raise AssertionError("test did not release pointer replacement")
        original_replace(source, destination)

    monkeypatch.setattr(store_module.os, "replace", blocked_replace)

    def record():
        try:
            store.record_release_success(lease, "3" * 40)
        except BaseException as exc:
            failures.append(exc)

    def control():
        try:
            Store(tmp_path / "state.db").control(task["id"], "pause")
        except BaseException as exc:
            failures.append(exc)
        finally:
            control_finished.set()

    writer = threading.Thread(target=record)
    writer.start()
    assert entered.wait(5)
    controller = threading.Thread(target=control)
    controller.start()
    assert not control_finished.wait(0.1)
    allow_replace.set()
    writer.join(5)
    controller.join(5)

    assert not writer.is_alive() and not controller.is_alive()
    assert failures == []
    assert store.get_task(task["id"])["state"] == "unresolved"
    kinds = [event["kind"] for event in store.events(task["id"])]
    assert kinds[-2:] == ["release_success", "pause"]
    assert (tmp_path / "releases" / "service" / "last-success.json").exists()


@pytest.mark.parametrize("commit_sha", ["", "A" * 40, "1" * 39, "1" * 41, "x" * 64])
def test_release_success_rejects_invalid_commit_before_file_mutation(tmp_path, commit_sha):
    store = make_store(tmp_path)
    _task, lease = monitoring_task(store)

    with pytest.raises(ValueError, match="commit_sha"):
        store.record_release_success(lease, commit_sha)

    assert not (tmp_path / "releases").exists()


@pytest.mark.parametrize("repository", ["org/repo", "../escape", "UPPER", ".hidden"])
def test_release_success_rejects_unsafe_repository_path(tmp_path, repository):
    store = make_store(tmp_path)
    _task, lease = monitoring_task(store, repository)

    with pytest.raises(ValueError, match="repository identifier"):
        store.record_release_success(lease, "4" * 40)

    assert not (tmp_path / "releases").exists()


def test_release_success_rejects_live_lease_outside_monitoring(tmp_path):
    store = make_store(tmp_path)
    task = store.create_task("service", {})
    lease = store.claim(task["id"], "worker")

    with pytest.raises(InvalidTransition, match="monitoring"):
        store.record_release_success(lease, "5" * 64)

    assert not (tmp_path / "releases").exists()


def ingest(
    store,
    delivery,
    payload=None,
    *,
    repository="org/repo",
    group="pull-request:17",
    digest=DIGEST_A,
    expected=None,
):
    if payload is None:
        payload = {"delivery": delivery}
    return store.ingest_event(
        "github",
        delivery,
        digest,
        repository,
        payload,
        group=group,
        expected_group_version=expected,
    )


def test_delivery_receipt_is_durable_and_exact_replay_does_not_create_work(tmp_path):
    store = make_store(tmp_path)
    original = ingest(store, "delivery-1", {"revision": "abc"}, expected=0)

    reopened = make_store(tmp_path)
    replay = reopened.ingest_event(
        "github",
        "delivery-1",
        DIGEST_A,
        "org/repo",
        {"revision": "ignored-on-replay"},
        group="different-group-is-not-reprocessed",
        expected_group_version=999,
    )

    assert original["duplicate"] is False
    assert replay["duplicate"] is True
    assert replay["task_id"] == original["task_id"]
    assert replay["task"]["payload"] == {"revision": "abc"}
    assert reopened.group_version("org/repo", "pull-request:17") == 1
    assert reopened.group_version("org/repo", "different-group-is-not-reprocessed") == 0
    assert len(reopened.list_tasks()) == 1
    lookup = reopened.get_event("github", "delivery-1")
    assert "duplicate" not in lookup
    assert lookup["task_id"] == original["task_id"]
    assert lookup["body_sha256"] == DIGEST_A


@pytest.mark.parametrize(
    ("digest", "repository"),
    [(DIGEST_B, "org/repo"), (DIGEST_A, "org/other")],
)
def test_delivery_identity_rejects_changed_body_or_repository(tmp_path, digest, repository):
    store = make_store(tmp_path)
    ingest(store, "delivery-1")

    with pytest.raises(EventConflict, match="different body or repository"):
        store.ingest_event(
            "github",
            "delivery-1",
            digest,
            repository,
            {"changed": True},
            group="pull-request:17",
        )

    assert len(store.list_tasks()) == 1
    assert store.group_version("org/repo", "pull-request:17") == 1


def test_new_delivery_id_for_same_replay_key_aliases_canonical_task(tmp_path):
    store = make_store(tmp_path)
    original = store.ingest_event(
        "github",
        "delivery-1",
        DIGEST_A,
        "org/repo",
        {"revision": "abc"},
        group="release:v1",
        expected_group_version=0,
        replay_key=REPLAY_A,
    )
    alias = store.ingest_event(
        "github",
        "delivery-2",
        DIGEST_A,
        "org/repo",
        None,
        group="ignored-on-replay",
        expected_group_version=999,
        replay_key=REPLAY_A,
    )

    assert alias["duplicate"] is True
    assert alias["delivery_id"] == "delivery-2"
    assert alias["task_id"] == original["task_id"]
    assert alias["group"] == "release:v1"
    assert alias["group_version"] == 1
    assert alias["replay_key"] == REPLAY_A
    assert len(store.list_tasks()) == 1
    assert store.group_version("org/repo", "release:v1") == 1
    assert store.group_version("org/repo", "ignored-on-replay") == 0
    canonical = store.get_replay("github", "org/repo", REPLAY_A)
    assert canonical["delivery_id"] == "delivery-1"
    assert canonical["task_id"] == original["task_id"]
    assert canonical["replay_key"] == REPLAY_A
    assert store.get_event("github", "delivery-2")["task_id"] == original["task_id"]


def test_replay_key_rejects_changed_body_without_creating_alias(tmp_path):
    store = make_store(tmp_path)
    store.ingest_event(
        "github",
        "delivery-1",
        DIGEST_A,
        "org/repo",
        {},
        replay_key=REPLAY_A,
    )

    with pytest.raises(EventConflict, match="different body"):
        store.ingest_event(
            "github",
            "delivery-2",
            DIGEST_B,
            "org/repo",
            None,
            replay_key=REPLAY_A,
        )

    with pytest.raises(EventNotFound):
        store.get_event("github", "delivery-2")


def test_same_delivery_rejects_changed_or_removed_persisted_replay_identity(tmp_path):
    store = make_store(tmp_path)
    store.ingest_event(
        "github",
        "delivery-1",
        DIGEST_A,
        "org/repo",
        {},
        replay_key=REPLAY_A,
    )

    with pytest.raises(EventConflict, match="different replay key"):
        store.ingest_event("github", "delivery-1", DIGEST_A, "org/repo", {})
    with pytest.raises(EventConflict, match="different replay key"):
        store.ingest_event(
            "github",
            "delivery-1",
            DIGEST_A,
            "org/repo",
            {},
            replay_key="d" * 64,
        )


def test_legacy_delivery_without_replay_key_still_deduplicates_after_upgrade(tmp_path):
    store = make_store(tmp_path)
    original = store.ingest_event("github", "delivery-1", DIGEST_A, "org/repo", {})

    replay = store.ingest_event(
        "github",
        "delivery-1",
        DIGEST_A,
        "org/repo",
        None,
        replay_key=REPLAY_A,
    )

    assert replay["duplicate"] is True
    assert replay["task_id"] == original["task_id"]
    assert replay["replay_key"] is None
    assert len(store.list_tasks()) == 1


def test_replay_key_is_scoped_by_provider_and_repository(tmp_path):
    store = make_store(tmp_path)
    first = store.ingest_event("github", "delivery-1", DIGEST_A, "org/one", {}, replay_key=REPLAY_A)
    second = store.ingest_event(
        "github", "delivery-2", DIGEST_A, "org/two", {}, replay_key=REPLAY_A
    )
    third = store.ingest_event("gitlab", "delivery-3", DIGEST_A, "org/one", {}, replay_key=REPLAY_A)

    assert len({first["task_id"], second["task_id"], third["task_id"]}) == 3


def test_group_cas_rejects_stale_lookup_without_recording_delivery(tmp_path):
    store = make_store(tmp_path)
    ingest(store, "delivery-1", expected=0)

    with pytest.raises(EventConflict, match="expected 0, found 1"):
        ingest(store, "delivery-2", expected=0)

    with pytest.raises(EventNotFound):
        store.get_event("github", "delivery-2")
    assert store.group_version("org/repo", "pull-request:17") == 1
    accepted = ingest(store, "delivery-2", expected=1)
    assert accepted["group_version"] == 2


def test_supersession_is_scoped_to_exact_repository_and_group(tmp_path):
    store = make_store(tmp_path)
    target = ingest(store, "target-1")
    other_group = ingest(store, "group-1", group="pull-request:18")
    other_repository = ingest(store, "repo-1", repository="org/other")
    direct = store.create_task("org/repo", {"source": "manual"})

    replacement = ingest(store, "target-2", expected=1)

    assert store.get_task(target["task_id"])["state"] == "cancelled"
    assert replacement["task"]["state"] == "queued"
    assert store.get_task(other_group["task_id"])["state"] == "queued"
    assert store.get_task(other_repository["task_id"])["state"] == "queued"
    assert store.get_task(direct["id"])["state"] == "queued"


def test_supersession_fences_active_lease_and_prevents_false_completion(tmp_path):
    store = make_store(tmp_path)
    first = ingest(store, "delivery-1")
    lease = store.claim(first["task_id"], "worker")
    store.transition(lease, "testing")

    replacement = ingest(store, "delivery-2", expected=1)

    superseded = store.get_task(first["task_id"])
    assert superseded["state"] == "cancelled"
    assert superseded["epoch"] > lease.epoch
    assert superseded["lease"] is None
    assert superseded["result"]["replacement_task_id"] == replacement["task_id"]
    with pytest.raises(StaleLease):
        store.transition(lease, "completed", {"false": "completion"})
    assert store.events(first["task_id"])[-1]["kind"] == "superseded"


@pytest.mark.parametrize("state", ["publishing", "deploying", "monitoring"])
def test_supersession_during_possible_external_effect_is_unresolved(tmp_path, state):
    store = make_store(tmp_path)
    first = ingest(store, "delivery-1")
    lease = store.claim(first["task_id"], "worker")
    store.transition(lease, state)

    replacement = ingest(store, "delivery-2", expected=1)

    superseded = store.get_task(first["task_id"])
    assert superseded["state"] == "unresolved"
    assert superseded["result"]["reconciliation_required"] is True
    assert superseded["result"]["previous_state"] == state
    with pytest.raises(StaleLease):
        store.assert_active(lease)
    with pytest.raises(LeaseConflict):
        store.claim(replacement["task_id"], "replacement-worker")
    assert replacement["task"]["state"] == "paused"
    created = store.events(replacement["task_id"])[0]
    assert created["details"]["reconciliation_required"] is True
    assert "unknown external outcome" in created["details"]["paused_reason"]
    with pytest.raises(InvalidTransition, match="terminal"):
        store.control(first["task_id"], "resume")
    store.control(replacement["task_id"], "resume")
    store.claim(replacement["task_id"], "replacement-worker")


def test_paused_group_creates_paused_replacement_until_operator_resumes(tmp_path):
    store = make_store(tmp_path)
    first = ingest(store, "delivery-1")
    store.control(first["task_id"], "pause")

    replacement = ingest(store, "delivery-2", expected=1)

    assert store.get_task(first["task_id"])["state"] == "cancelled"
    assert replacement["task"]["state"] == "paused"
    with pytest.raises(LeaseConflict):
        store.claim(replacement["task_id"], "worker")
    resumed = store.control(replacement["task_id"], "resume")
    assert resumed["state"] == "queued"
    store.claim(replacement["task_id"], "worker")


def test_group_tombstone_cancels_work_advances_version_and_creates_no_task(tmp_path):
    store = make_store(tmp_path)
    first = ingest(store, "delivery-1")

    closed = store.ingest_event(
        "github",
        "delivery-close",
        DIGEST_B,
        "org/repo",
        None,
        group="pull-request:17",
        expected_group_version=1,
    )

    assert closed["duplicate"] is False
    assert closed["task"] is None
    assert closed["task_id"] is None
    assert closed["group_version"] == 2
    assert store.group_version("org/repo", "pull-request:17") == 2
    assert store.get_task(first["task_id"])["state"] == "cancelled"
    assert len(store.list_tasks()) == 1
    assert store.get_event("github", "delivery-close")["task"] is None


def test_group_tombstone_during_external_effect_requires_reconciliation(tmp_path):
    store = make_store(tmp_path)
    first = ingest(store, "delivery-1")
    lease = store.claim(first["task_id"], "worker")
    store.transition(lease, "deploying")

    store.ingest_event(
        "github",
        "delivery-close",
        DIGEST_B,
        "org/repo",
        None,
        group="pull-request:17",
        expected_group_version=1,
    )

    closed = store.get_task(first["task_id"])
    assert closed["state"] == "unresolved"
    assert closed["result"]["replacement_task_id"] is None
    assert closed["result"]["reconciliation_required"] is True
    with pytest.raises(StaleLease):
        store.transition(lease, "completed")


def test_ungrouped_ignored_receipt_has_no_task_or_group_version(tmp_path):
    store = make_store(tmp_path)

    receipt = store.ingest_event("github", "ignored-1", DIGEST_A, "org/repo", None)

    assert receipt["task"] is None
    assert receipt["group"] is None
    assert receipt["group_version"] is None
    assert store.list_tasks() == []


def test_replaying_superseded_delivery_returns_original_without_touching_latest(tmp_path):
    store = make_store(tmp_path)
    first = ingest(store, "delivery-1")
    latest = ingest(store, "delivery-2", expected=1)

    replay = ingest(store, "delivery-1", {"ignored": True}, expected=0)

    assert replay["duplicate"] is True
    assert replay["task_id"] == first["task_id"]
    assert replay["task"]["state"] == "cancelled"
    assert store.get_task(latest["task_id"])["state"] == "queued"
    assert store.group_version("org/repo", "pull-request:17") == 2


def test_simultaneous_replay_creates_exactly_one_task_and_receipt(tmp_path):
    path = tmp_path / "state.db"
    Store(path)
    barrier = threading.Barrier(3)
    outcomes = []
    failures = []

    def attempt():
        candidate = Store(path)
        barrier.wait()
        try:
            outcomes.append(ingest(candidate, "delivery-1", expected=0))
        except BaseException as exc:
            failures.append(exc)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert failures == []
    assert sorted(outcome["duplicate"] for outcome in outcomes) == [False, True]
    assert len({outcome["task_id"] for outcome in outcomes}) == 1
    assert len(Store(path).list_tasks()) == 1


def test_simultaneous_group_cas_allows_only_one_new_version(tmp_path):
    path = tmp_path / "state.db"
    store = Store(path)
    ingest(store, "delivery-1", expected=0)
    barrier = threading.Barrier(3)
    accepted = []
    conflicts = []

    def attempt(delivery):
        candidate = Store(path)
        barrier.wait()
        try:
            accepted.append(ingest(candidate, delivery, expected=1))
        except EventConflict as exc:
            conflicts.append(exc)

    threads = [
        threading.Thread(target=attempt, args=(delivery,))
        for delivery in ("delivery-2", "delivery-3")
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert len(accepted) == 1
    assert len(conflicts) == 1
    assert Store(path).group_version("org/repo", "pull-request:17") == 2


def test_simultaneous_replay_aliases_create_one_task_and_one_group_version(tmp_path):
    path = tmp_path / "state.db"
    Store(path)
    barrier = threading.Barrier(3)
    outcomes = []
    failures = []

    def attempt(delivery):
        candidate = Store(path)
        barrier.wait()
        try:
            outcomes.append(
                candidate.ingest_event(
                    "github",
                    delivery,
                    DIGEST_A,
                    "org/repo",
                    {"delivery": delivery},
                    group="release:v1",
                    expected_group_version=0,
                    replay_key=REPLAY_A,
                )
            )
        except BaseException as exc:
            failures.append(exc)

    threads = [
        threading.Thread(target=attempt, args=(delivery,))
        for delivery in ("delivery-1", "delivery-2")
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert failures == []
    assert sorted(outcome["duplicate"] for outcome in outcomes) == [False, True]
    assert len({outcome["task_id"] for outcome in outcomes}) == 1
    reopened = Store(path)
    assert len(reopened.list_tasks()) == 1
    assert reopened.group_version("org/repo", "release:v1") == 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"provider": ""},
        {"provider": " github"},
        {"delivery_id": "x" * 257},
        {"body_sha256": "A" * 64},
        {"body_sha256": "a" * 63},
        {"repository": "org/repo\n"},
        {"group": "bad\x00group"},
        {"expected_group_version": -1},
        {"expected_group_version": True},
        {"replay_key": "C" * 64},
        {"replay_key": "c" * 63},
    ],
)
def test_event_identifiers_digest_and_version_are_bounded(tmp_path, kwargs):
    values = {
        "provider": "github",
        "delivery_id": "delivery-1",
        "body_sha256": DIGEST_A,
        "repository": "org/repo",
        "payload": {},
        "group": "pull-request:17",
        "expected_group_version": 0,
    }
    values.update(kwargs)

    with pytest.raises(ValueError):
        make_store(tmp_path).ingest_event(**values)


def test_expected_version_without_group_and_missing_receipt_fail_closed(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(ValueError, match="requires group"):
        store.ingest_event(
            "github", "delivery-1", DIGEST_A, "org/repo", {}, expected_group_version=0
        )
    with pytest.raises(EventNotFound):
        store.get_event("github", "missing")
    with pytest.raises(EventNotFound):
        store.get_replay("github", "org/repo", REPLAY_A)


def test_receipt_schema_stores_only_digest_routing_and_task_reference(tmp_path):
    path = tmp_path / "state.db"
    store = Store(path)
    receipt = ingest(store, "delivery-1", {"safe": "normalized"})

    with sqlite3.connect(path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(provider_deliveries)")}
        row = connection.execute(
            "SELECT * FROM provider_deliveries WHERE provider = 'github'"
        ).fetchone()

    assert columns == {
        "provider",
        "delivery_id",
        "body_sha256",
        "repository",
        "group_key",
        "group_version",
        "task_id",
        "created_at",
    }
    assert row is not None
    assert receipt["task_id"] in row


def test_reopening_an_existing_store_adds_event_tables_without_changing_tasks(tmp_path):
    path = tmp_path / "state.db"
    store = Store(path)
    task = store.create_task("org/repo", {"existing": True})
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE provider_delivery_replays")
        connection.execute("DROP TABLE provider_replays")
        connection.execute("DROP TABLE provider_deliveries")
        connection.execute("DROP TABLE event_task_groups")
        connection.execute("DROP TABLE event_groups")

    migrated = Store(path)

    assert migrated.get_task(task["id"]) == task
    receipt = ingest(migrated, "delivery-1", expected=0)
    assert receipt["task"]["state"] == "queued"
