"""Durable task state, control commands, and fenced execution leases."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

TERMINAL_STATES = frozenset({"completed", "failed", "cancelled", "unresolved"})
UNKNOWN_OUTCOME_STATES = frozenset({"publishing", "deploying", "monitoring"})
WORKFLOW_STATES = frozenset(
    {
        "queued",
        "discovered",
        "preparing",
        "planning",
        "testing",
        "diagnosing",
        "executing",
        "verifying",
        "repairing",
        "reviewing",
        "eligible",
        "merging",
        "publishing",
        "deploying",
        "releasing",
        "monitoring",
        *TERMINAL_STATES,
    }
)
CONTROL_ACTIONS = frozenset({"pause", "resume", "cancel", "steer"})


class StoreError(RuntimeError):
    """Base error for rejected store operations."""


class TaskNotFound(KeyError):
    """The requested task does not exist."""


class LeaseConflict(StoreError):
    """A task already has a live lease or cannot currently be claimed."""


class StaleLease(StoreError):
    """A lease has expired or was fenced by a newer epoch."""


class InvalidTransition(StoreError):
    """A task state or control transition is invalid."""


@dataclass(frozen=True)
class Lease:
    task_id: str
    worker: str
    epoch: int
    expires_at: float


class Store:
    """A process-safe SQLite control store.

    The store is deliberately file backed. Each public operation opens its own
    connection so Store instances can be shared by threads and separate
    processes without sharing sqlite connection objects.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    repository TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    instructions_json TEXT NOT NULL DEFAULT '[]',
                    result_json TEXT,
                    idempotency_key TEXT UNIQUE,
                    state TEXT NOT NULL,
                    control_epoch INTEGER NOT NULL DEFAULT 0,
                    lease_worker TEXT,
                    lease_epoch INTEGER,
                    lease_expires_at REAL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    CHECK (
                        (lease_worker IS NULL AND lease_epoch IS NULL
                            AND lease_expires_at IS NULL)
                        OR
                        (lease_worker IS NOT NULL AND lease_epoch IS NOT NULL
                            AND lease_expires_at IS NOT NULL)
                    )
                );

                CREATE TABLE IF NOT EXISTS task_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL REFERENCES tasks(id),
                    kind TEXT NOT NULL,
                    state TEXT NOT NULL,
                    epoch INTEGER NOT NULL,
                    details_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS task_events_task_order
                    ON task_events(task_id, event_id);
                CREATE INDEX IF NOT EXISTS tasks_expired_leases
                    ON tasks(lease_expires_at)
                    WHERE lease_expires_at IS NOT NULL;
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=30,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()

    @staticmethod
    def _json(value: object) -> str:
        try:
            return json.dumps(
                value,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("value must be JSON serializable") from exc

    @staticmethod
    def _task(row: sqlite3.Row) -> dict[str, Any]:
        lease: dict[str, Any] | None = None
        if row["lease_worker"] is not None:
            lease = {
                "worker": row["lease_worker"],
                "epoch": row["lease_epoch"],
                "expires_at": row["lease_expires_at"],
            }
        return {
            "id": row["id"],
            "repository": row["repository"],
            "payload": json.loads(row["payload_json"]),
            "instructions": json.loads(row["instructions_json"]),
            "result": json.loads(row["result_json"]) if row["result_json"] is not None else None,
            "idempotency_key": row["idempotency_key"],
            "state": row["state"],
            "epoch": row["control_epoch"],
            "lease": lease,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _required_task(connection: sqlite3.Connection, task_id: str) -> sqlite3.Row:
        row = cast(
            sqlite3.Row | None,
            connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone(),
        )
        if row is None:
            raise TaskNotFound(task_id)
        return row

    def _event(
        self,
        connection: sqlite3.Connection,
        task_id: str,
        kind: str,
        state: str,
        epoch: int,
        details: object,
        created_at: float,
    ) -> None:
        connection.execute(
            """
            INSERT INTO task_events(task_id, kind, state, epoch, details_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (task_id, kind, state, epoch, self._json(details), created_at),
        )

    @staticmethod
    def _validate_ttl(ttl_seconds: int) -> None:
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be a positive integer")

    @staticmethod
    def _validate_lease(row: sqlite3.Row, lease: Lease, now: float) -> None:
        if (
            row["id"] != lease.task_id
            or row["lease_worker"] != lease.worker
            or row["lease_epoch"] != lease.epoch
            or row["control_epoch"] != lease.epoch
            or row["lease_expires_at"] is None
            or row["lease_expires_at"] <= now
        ):
            raise StaleLease(f"lease for task {lease.task_id!r} is no longer active")

    def create_task(
        self,
        repository: str,
        payload: dict[str, Any],
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if not repository.strip():
            raise ValueError("repository must not be empty")
        if not isinstance(payload, dict):
            raise ValueError("payload must be a JSON object")
        payload_json = self._json(payload)
        now = time.time()
        with self._write() as connection:
            if idempotency_key is not None:
                existing = connection.execute(
                    "SELECT * FROM tasks WHERE idempotency_key = ?", (idempotency_key,)
                ).fetchone()
                if existing is not None:
                    if (
                        existing["repository"] != repository
                        or existing["payload_json"] != payload_json
                    ):
                        raise ValueError(
                            "idempotency key is already associated with a different task"
                        )
                    return self._task(existing)

            task_id = uuid.uuid4().hex
            connection.execute(
                """
                INSERT INTO tasks(
                    id, repository, payload_json, instructions_json, result_json, idempotency_key,
                    state, control_epoch, created_at, updated_at
                ) VALUES (?, ?, ?, '[]', NULL, ?, 'queued', 0, ?, ?)
                """,
                (task_id, repository, payload_json, idempotency_key, now, now),
            )
            self._event(connection, task_id, "created", "queued", 0, {}, now)
            return self._task(self._required_task(connection, task_id))

    def get_task(self, task_id: str) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            return self._task(self._required_task(connection, task_id))

    def list_tasks(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute("SELECT * FROM tasks ORDER BY created_at, id").fetchall()
            return [self._task(row) for row in rows]

    def events(self, task_id: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            self._required_task(connection, task_id)
            rows = connection.execute(
                "SELECT * FROM task_events WHERE task_id = ? ORDER BY event_id", (task_id,)
            ).fetchall()
        return [
            {
                "id": row["event_id"],
                "task_id": row["task_id"],
                "kind": row["kind"],
                "state": row["state"],
                "epoch": row["epoch"],
                "details": json.loads(row["details_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def claim(self, task_id: str, worker: str, ttl_seconds: int = 60) -> Lease:
        self._validate_ttl(ttl_seconds)
        if not worker.strip():
            raise ValueError("worker must not be empty")
        now = time.time()
        expires_at = now + ttl_seconds
        with self._write() as connection:
            row = self._required_task(connection, task_id)
            if row["state"] != "queued":
                raise LeaseConflict(f"task {task_id!r} is in state {row['state']!r}")
            if row["lease_expires_at"] is not None and row["lease_expires_at"] > now:
                raise LeaseConflict(f"task {task_id!r} already has a live lease")
            repository_owner = connection.execute(
                """
                SELECT id FROM tasks
                WHERE repository = ? AND id <> ? AND lease_expires_at > ?
                LIMIT 1
                """,
                (row["repository"], task_id, now),
            ).fetchone()
            if repository_owner is not None:
                raise LeaseConflict(
                    f"repository {row['repository']!r} already has a live task lease"
                )

            previous_worker = row["lease_worker"]
            epoch = int(row["control_epoch"]) + 1
            connection.execute(
                """
                UPDATE tasks
                SET control_epoch = ?, lease_worker = ?, lease_epoch = ?,
                    lease_expires_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (epoch, worker, epoch, expires_at, now, task_id),
            )
            details: dict[str, Any] = {"worker": worker, "expires_at": expires_at}
            if previous_worker is not None:
                details["replaced_expired_worker"] = previous_worker
            self._event(connection, task_id, "claimed", "queued", epoch, details, now)
        return Lease(task_id, worker, epoch, expires_at)

    def renew(self, lease: Lease, ttl_seconds: int = 60) -> Lease:
        self._validate_ttl(ttl_seconds)
        now = time.time()
        expires_at = now + ttl_seconds
        with self._write() as connection:
            row = self._required_task(connection, lease.task_id)
            self._validate_lease(row, lease, now)
            connection.execute(
                "UPDATE tasks SET lease_expires_at = ?, updated_at = ? WHERE id = ?",
                (expires_at, now, lease.task_id),
            )
            self._event(
                connection,
                lease.task_id,
                "lease_renewed",
                row["state"],
                lease.epoch,
                {"worker": lease.worker, "expires_at": expires_at},
                now,
            )
        return Lease(lease.task_id, lease.worker, lease.epoch, expires_at)

    def assert_active(self, lease: Lease) -> None:
        now = time.time()
        with closing(self._connect()) as connection:
            row = self._required_task(connection, lease.task_id)
            self._validate_lease(row, lease, now)
            if row["state"] in TERMINAL_STATES or row["state"] == "paused":
                raise StaleLease(f"task {lease.task_id!r} cannot be executed")

    def reserve_ai_call(self, lease: Lease, role: str, limit: int) -> int:
        """Charge the durable task budget before dispatch, including retries across epochs."""
        self._validate_ttl(limit)
        now = time.time()
        with self._write() as connection:
            row = self._required_task(connection, lease.task_id)
            self._validate_lease(row, lease, now)
            count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'ai_call_reserved'",
                    (lease.task_id,),
                ).fetchone()[0]
            )
            if count >= limit:
                raise StoreError("Durable AI task call budget exhausted")
            self._event(
                connection,
                lease.task_id,
                "ai_call_reserved",
                row["state"],
                lease.epoch,
                {"role": role, "call": count + 1, "limit": limit},
                now,
            )
            return count + 1

    def transition(
        self,
        lease: Lease,
        state: str,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if state not in WORKFLOW_STATES or state == "queued":
            raise InvalidTransition(f"unsupported worker transition to {state!r}")
        if details is not None and not isinstance(details, dict):
            raise ValueError("details must be a JSON object")
        details_json = self._json(details or {})
        now = time.time()
        with self._write() as connection:
            row = self._required_task(connection, lease.task_id)
            self._validate_lease(row, lease, now)
            if row["state"] in TERMINAL_STATES:
                raise InvalidTransition(f"task {lease.task_id!r} is already terminal")
            if row["state"] == "paused":
                raise InvalidTransition(f"task {lease.task_id!r} is paused")

            terminal = state in TERMINAL_STATES
            connection.execute(
                """
                UPDATE tasks
                SET state = ?, result_json = CASE WHEN ? THEN ? ELSE result_json END,
                    lease_worker = CASE WHEN ? THEN NULL ELSE lease_worker END,
                    lease_epoch = CASE WHEN ? THEN NULL ELSE lease_epoch END,
                    lease_expires_at = CASE WHEN ? THEN NULL ELSE lease_expires_at END,
                    updated_at = ?
                WHERE id = ?
                """,
                (state, terminal, details_json, terminal, terminal, terminal, now, lease.task_id),
            )
            self._event(
                connection,
                lease.task_id,
                "transition",
                state,
                lease.epoch,
                json.loads(details_json),
                now,
            )
            return self._task(self._required_task(connection, lease.task_id))

    def release(self, lease: Lease) -> None:
        now = time.time()
        with self._write() as connection:
            row = self._required_task(connection, lease.task_id)
            self._validate_lease(row, lease, now)
            if row["state"] in TERMINAL_STATES or row["state"] == "paused":
                raise StaleLease(f"task {lease.task_id!r} cannot be released")
            connection.execute(
                """
                UPDATE tasks
                SET state = 'queued', lease_worker = NULL, lease_epoch = NULL,
                    lease_expires_at = NULL, updated_at = ?
                WHERE id = ?
                """,
                (now, lease.task_id),
            )
            self._event(
                connection,
                lease.task_id,
                "released",
                "queued",
                lease.epoch,
                {"worker": lease.worker, "previous_state": row["state"]},
                now,
            )

    def control(
        self,
        task_id: str,
        action: Literal["pause", "resume", "cancel", "steer"],
        instruction: str | None = None,
    ) -> dict[str, Any]:
        if action not in CONTROL_ACTIONS:
            raise ValueError(f"unsupported control action {action!r}")
        if action == "steer" and (instruction is None or not instruction.strip()):
            raise ValueError("steer requires a non-empty instruction")
        if action != "steer" and instruction is not None:
            raise ValueError("instruction is only valid for steer")

        now = time.time()
        with self._write() as connection:
            row = self._required_task(connection, task_id)
            previous_state = str(row["state"])
            if previous_state in TERMINAL_STATES:
                raise InvalidTransition(f"task {task_id!r} is already terminal")
            if action == "resume" and previous_state != "paused":
                raise InvalidTransition("only a paused task can be resumed")

            unknown_outcome = previous_state in UNKNOWN_OUTCOME_STATES and action in {
                "pause",
                "cancel",
                "steer",
            }
            if unknown_outcome:
                state = "unresolved"
            elif action == "pause":
                state = "paused"
            elif action == "cancel":
                state = "cancelled"
            else:
                state = "queued"

            instructions_json = str(row["instructions_json"])
            if action == "steer":
                instructions = json.loads(instructions_json)
                instructions.append(instruction)
                instructions_json = self._json(instructions)

            epoch = int(row["control_epoch"]) + 1
            result: dict[str, Any] | None = None
            if unknown_outcome:
                result = {
                    "reason": "control interrupted a stage with possible external effects",
                    "control": action,
                    "previous_state": previous_state,
                    "reconciliation_required": True,
                }
            elif action == "cancel":
                result = {"control": "cancel", "previous_state": previous_state}
            connection.execute(
                """
                UPDATE tasks
                SET state = ?, control_epoch = ?, instructions_json = ?,
                    result_json = COALESCE(?, result_json),
                    lease_worker = NULL, lease_epoch = NULL, lease_expires_at = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    state,
                    epoch,
                    instructions_json,
                    self._json(result) if result is not None else None,
                    now,
                    task_id,
                ),
            )
            details: dict[str, Any] = {"previous_state": previous_state}
            if instruction is not None:
                details["instruction"] = instruction
            if unknown_outcome:
                details["reconciliation_required"] = True
            self._event(connection, task_id, action, state, epoch, details, now)
            return self._task(self._required_task(connection, task_id))

    def recover_expired(self) -> list[str]:
        now = time.time()
        recovered: list[str] = []
        with self._write() as connection:
            rows = connection.execute(
                """
                SELECT * FROM tasks
                WHERE lease_expires_at IS NOT NULL AND lease_expires_at <= ?
                ORDER BY created_at, id
                """,
                (now,),
            ).fetchall()
            for row in rows:
                task_id = str(row["id"])
                if row["state"] in TERMINAL_STATES or row["state"] == "paused":
                    continue
                epoch = int(row["control_epoch"]) + 1
                unknown_outcome = row["state"] in UNKNOWN_OUTCOME_STATES
                recovered_state = "unresolved" if unknown_outcome else "queued"
                result = (
                    self._json(
                        {
                            "reason": "lease expired during a stage with possible external effects",
                            "previous_state": row["state"],
                            "reconciliation_required": True,
                        }
                    )
                    if unknown_outcome
                    else None
                )
                connection.execute(
                    """
                    UPDATE tasks
                    SET state = ?, control_epoch = ?, result_json = COALESCE(?, result_json),
                        lease_worker = NULL,
                        lease_epoch = NULL, lease_expires_at = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (recovered_state, epoch, result, now, task_id),
                )
                self._event(
                    connection,
                    task_id,
                    "lease_expired",
                    recovered_state,
                    epoch,
                    {
                        "worker": row["lease_worker"],
                        "previous_state": row["state"],
                        "expired_at": row["lease_expires_at"],
                        "reconciliation_required": unknown_outcome,
                    },
                    now,
                )
                recovered.append(task_id)
        return recovered
