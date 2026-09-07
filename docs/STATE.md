# Durable task and control state

`katydid.store.Store` is a file-backed SQLite control store for autonomous tasks. Creating a
store initializes its schema and enables WAL mode, foreign keys, and a 30-second busy timeout.
Each operation uses an independent connection and authority-changing operations use an immediate
transaction, so instances can safely coordinate across threads and processes.

## Task records

`create_task(repository, payload, idempotency_key=None)` creates a task in `queued` state. The
returned dictionary contains `id`, `repository`, the JSON `payload`, `instructions`, `state`,
`epoch`, current `lease`, latest terminal `result`, the optional idempotency key, and numeric UTC
`created_at`/`updated_at` timestamps. `get_task`, `list_tasks`, and `events` return fresh
JSON-compatible values.

An idempotency key can be replayed only with the same repository and canonically equivalent JSON
payload. Such a replay returns the original task and does not append another event. Reusing the
key for different input is rejected.

The event log is append-only through the public API. Creation, claims, renewals, state changes,
releases, control commands, and lease recovery are appended in the same transaction as their task
update. Events include the resulting state and epoch plus operation details.

## Leases and worker transitions

`claim(task_id, worker, ttl_seconds=60)` grants an exclusive `Lease` only for a queued task and
only when no other task for the same repository has a live lease. The repository check and lease
grant share one immediate transaction, preventing races between separate task IDs. A lease
contains the task ID, worker identity, fencing epoch, and Unix expiry timestamp. Workers must call
`assert_active` immediately before a state-changing external action and renew long work with
`renew`. Every new grant advances the epoch. This prevents a released or expired lease from
becoming valid again, including when the same worker later reclaims the task.

Fleet loading also rejects multiple repository IDs that normalize to the same Git source. This
keeps aliases from bypassing repository-level lease serialization.

Workers use `transition` for pipeline states including `preparing`, `testing`, `diagnosing`,
`repairing`, `verifying`, `reviewing`, `publishing`, `deploying`, and `monitoring`, or the terminal
states `completed`, `failed`, `cancelled`, and `unresolved`. Repeated repair, verification, and
review cycles are valid. A terminal transition clears the lease and stores its details as the
task's latest `result`. `release` relinquishes nonterminal work and queues it for another claim.
Terminal tasks reject further claims, controls, and transitions.

`recover_expired()` atomically fences expired workers and returns affected task IDs. Pre-side-effect
work is queued again. Expiry during `publishing`, `deploying`, or `monitoring` instead records an
`unresolved` result requiring external reconciliation, because completion may have occurred before
the worker lost authority. Recovery never declares an interrupted operation completed.

## Pause, resume, cancel, and steering

Every accepted control command is durable before it returns, advances the epoch, and revokes the
current lease.

- `pause` sets `paused`; claims and automatic expiry recovery cannot resume it.
- `resume` is accepted only for `paused` and explicitly returns it to `queued`.
- `cancel` sets the terminal `cancelled` state.
- `steer` requires a non-empty instruction, appends it to the task's top-level `instructions`
  list, and queues the task for replanning. The submitted payload remains unchanged.

If pause, cancel, or steer interrupts `publishing`, `deploying`, or `monitoring`, the task becomes
`unresolved` with `reconciliation_required` in its result and event details. These stages may have
committed an external effect before interruption. The terminal unresolved state prevents resume or
another steering command from blindly replaying that effect.

An already-running worker observes every control action as a stale lease. This fencing prevents it
from recording a later success after interruption; it does not imply that an external operation
already in flight was reversed.

## Controller integration coverage

The controller integration suite creates real temporary Git sources outside the state directory.
Each source contains a seeded failing implementation and a protected verifier that imports the
implementation, executes an assertion, and writes JUnit evidence. A deterministic test double is
injected only for structured diagnosis, repair, and independent review responses.

The suite proves the complete baseline-failure, repair, verification, review, local publication,
and fast-forward merge path. It also covers repository isolation, review rejection, edits outside
the central allowlist, missing JUnit evidence, durable cancellation during an AI call, source-head
changes after enqueue, and real release hooks where failed health initiates rollback and a second
health check. Git heads, protected files, task events, evidence gates, and release files are
checked as external outcomes rather than inferred from controller return labels.

Additional regressions require rapid equal-size Python repairs to execute fresh source, reject
workspace mutations omitted from the structured edit manifest, requeue early work on worker
shutdown, and distinguish a known pre-publication base move from an uncertain external outcome.
