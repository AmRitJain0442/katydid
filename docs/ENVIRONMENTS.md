# Run-owned environments

Katydid profiles may declare a bounded local environment lifecycle around their ordinary checks.
Each run gets a new directory under its evidence folder. The runner expands `{environment}` to the
absolute `environment/data` path and `{run_id}` to the run identifier; it also provides the same
values as `KATYDID_ENVIRONMENT_DIR` and `KATYDID_RUN_ID`.

The lifecycle executes in this order:

```text
create unique directory
  → ordered prepare commands
  → readiness probe until ready or deadline
  → selected test/command checks
  → all cleanup commands
  → final aggregate gate
```

Tests do not start until every prepare command succeeds and readiness passes. Cleanup begins after
setup has started and runs after success, test failure, timeout, ordinary cancellation, partial
preparation, or an unexpected runner exception. A run cannot pass while cleanup is incomplete or
failed.

## Profile contract

The SQLite example in `examples/sqlite-service/quality.yaml` contains the complete syntax:

```yaml
environment:
  prepare:
    - id: sqlite-prepare
      kind: command
      argv: ["{python}", "ops.py", "prepare", "{environment}"]
      timeout_seconds: 20
      required: true
  readiness:
    check:
      id: sqlite-ready
      kind: command
      argv: ["{python}", "ops.py", "ready", "{environment}"]
      timeout_seconds: 5
      required: true
    timeout_seconds: 15
    interval_seconds: 0.25
    max_attempts: 5
  cleanup:
    - id: sqlite-cleanup
      kind: command
      argv: ["{python}", "ops.py", "cleanup", "{environment}"]
      timeout_seconds: 10
      required: true
```

Every lifecycle hook must be a required `command`. Hook IDs are unique across prepare, readiness,
and cleanup. A hook cannot declare `stages`: the selected run has one lifecycle shared by all its
selected checks, so filtering a preparation or cleanup command by stage would make ownership and
disposal ambiguous. Ordinary checks keep their existing stage selection and may use
`{environment}` only when the profile declares a lifecycle.

Readiness has two bounds. The probe's `timeout_seconds` limits a single attempt, while the outer
readiness timeout limits the whole retry period; `interval_seconds` controls the bounded pause
between attempts. `max_attempts` adds a separate attempt-count bound (20 by default, 5 in the
example). Readiness stops at whichever outer bound is reached first. Only the declared readiness
probe is retried. Its original result, output, and invocation are retained for every attempt.

## SQLite example

The example uses Python's standard-library `sqlite3` module and pytest already present in
Katydid's locked development environment. It has no background service, Docker daemon, network
dependency, credential, or external cleanup process.

Run it from the repository root:

```text
python scripts/dev.py cli validate examples/sqlite-service/quality.yaml
python scripts/dev.py cli plan examples/sqlite-service/quality.yaml
python scripts/dev.py cli run examples/sqlite-service/quality.yaml
```

Preparation writes an ownership marker, creates `app.sqlite`, applies schema version 1, and seeds
three inventory products. Readiness opens the database read-only and verifies its schema version,
integrity result, run identity, row count, and initial stock total. The required pytest check then
uses the actual database to verify:

- a confirmed order atomically decrements inventory and records the correct line price;
- insufficient stock persists a rejected order while leaving inventory unchanged and creating no
  order line;
- metadata, orders, and the database path belong only to the current run.

The test command writes three real test cases to Katydid's fresh `{report}` JUnit path. A later run
gets another directory and a freshly migrated and seeded database, so baseline and verification
runs cannot reuse each other's orders or state.

## Evidence and diagnosis

A run with an environment adds this evidence structure:

```text
RUN/environment/
  environment.json
  data/
    katydid-environment.json
  prepare-000-sqlite-prepare/
    invocation.json  stdout.log  stderr.log
  readiness-000-sqlite-ready/
    invocation.json  stdout.log  stderr.log
  cleanup-000-sqlite-cleanup/
    invocation.json  stdout.log  stderr.log
```

`run.json` records the environment directory, current phase, whether setup started and became
ready, whether cleanup completed, each lifecycle result, and any lifecycle error. The ordinary
check evidence and aggregate gate remain alongside it.

Preparation, readiness, and cleanup failures are infrastructure outcomes. In controller work they
stop before AI diagnosis or repair, because changing application files cannot make a failed
run-owned environment trustworthy. A failed application check after successful readiness retains
its test outcome, runs cleanup, and may then enter the normal diagnosis path.

## Cleanup boundary

The SQLite cleanup command is deliberately narrower than deleting its environment directory. It
first resolves the argv path and `KATYDID_ENVIRONMENT_DIR`, requires them to identify the same real
directory, and checks that `katydid-environment.json` contains the exact run ID and absolute path.
It then considers only these names:

```text
app.sqlite
app.sqlite-wal
app.sqlite-shm
app.sqlite-journal
```

Each existing target must be a regular file rather than a symlink or reparse point. Missing files
are successful no-ops, so cleanup can be repeated after partial setup or a previous cleanup. The
ownership marker stays with the run evidence and lets later cleanup attempts prove scope. Cleanup
does not recursively remove a supplied path or touch unknown files.

The runner records the unique environment data path before setup and reuses that stored path for
cleanup; it does not recalculate cleanup scope from mutable check output. Cleanup commands have
their own timeouts and ignore the cancellation flag that stopped ordinary work. All declared
cleanup commands are attempted even when an earlier one fails. This covers cooperative
cancellation and graceful service shutdown, where the worker remains alive long enough to execute
its `finally` path.

A hard-killed process or host cannot execute in-process cleanup. The run remains nonpassing with
its last checkpoint and ownership evidence, but automatic expiry reconciliation and an independent
sweeper are future provider-specific work. This local lifecycle does not claim container, cloud,
credential, network, or tenant isolation.
