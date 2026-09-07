# Implementation record

## First milestone

Deliver a local, reproducible path from a repository profile to an explicit test plan, bounded execution, original evidence, and an aggregate outcome that cannot pass on missing tests.

This is a foundation for the blueprint's Phase 1. AI hunting, automatic patch/merge/deployment, remote sandboxing, and durable fleet-wide interruption are not part of this initial slice.

## Decisions

- Python 3.12, a `src` package, and a small standard-library CLI keep the core testable without a server.
- Pydantic validates strict profile contracts; YAML is an authoring format, not an arbitrary object loader.
- Existing commands remain usable through argument arrays. No implicit shell execution or dependency installation occurs during a run.
- Real process outcomes and test evidence remain distinct. An exit code of zero is insufficient when a check declares test evidence.
- Generated artifacts live outside source control, and every invocation gets its own directory.
- A cross-platform pinned development helper avoids reliance on the machine's default Python version or global uv version.

## Microcommits

### 1. Pinned environment and package foundation

Added the installable CLI, exact Python/uv selection, dependency lock, isolated development commands, and environment documentation. At this commit the CLI exposed help and version only. Validation: locked sync, CLI version, lint, formatting, and strict type checking.

### 2. Strict profiles and deterministic planning

Added the implemented v1 profile, command argument arrays, explicit stage selection/exclusions, source hashes, strict YAML/Pydantic validation, and resolved working-directory containment. `validate` and `plan` execute no repository code. Added negative tests for ambiguous configuration, invalid types, path escape, empty selections, and duplicate IDs. Windows cannot create the symlink fixture without additional privileges; Linux CI will exercise that case.

### 3. Test evidence and aggregate gates

Added bounded, hardened JUnit parsing and explicit missing/invalid/zero/skipped/failing outcomes. Declared counts must match real testcases; orphan errors cannot disappear behind passing summaries. Aggregation reconciles all planned checks and rejects missing, unexpected, duplicate, or unsupported passing outcomes. Advisory failures remain visible. Tests cover real failures, malformed XML, external-entity declarations, nested suites, all-skipped execution, and missing evidence.

### 4. Local runner, checkpoints, and cancellation

Added fresh per-run/check artifacts, expanded argument arrays, exact profile hash checks, runtime/Git identity, process timeouts, cooperative cancellation, a soft log limit, and atomic JSON checkpoints. `run` and `cancel` are available in the CLI. Running/cancelled checkpoints cannot pass. Integration tests execute real subprocesses, verify child cleanup at timeout, reject stale evidence, preserve failures while later checks run, and cover launch errors, cancellation, paths with spaces, literal shell text, and exit codes. The local trust and foreground-process requirements are documented explicitly.

### 5. Self-testing profile, example, and CI

Added a real `katydid.yaml` that executes lint, formatting, strict typing, and pytest through Katydid. Added a separate pricing example with its own profile and JUnit-producing tests. GitHub Actions validates Ubuntu 24.04 and Windows Server 2022 using pinned action commits, locked project dependencies, read-only permissions, and seven-day evidence retention. The README distinguishes implemented capabilities from the blueprint. Local validation passed the self-profile, example profile, and distribution build; hosted CI is the authority for cross-platform results on each pushed revision.

### 6. Launcher and interruption regression checks

Rejected direct batch launchers to avoid Windows' implicit shell behaviour and documented explicit-shell alternatives. Added a process-level regression that requests cancellation from a second CLI invocation and verifies terminal state, nonpassing evidence, and exit code 130. This supplements the in-process cancellation and process-tree timeout tests.

## End-to-end autonomous milestone

The following slices extend the foundation into a working single-host fleet controller. See the [acceptance plan](END_TO_END_PLAN.md), [live evidence](LIVE_ACCEPTANCE.md), and [operator runbook](RUNBOOK.md). Red teaming is deferred.

| Microcommit | Implementation and verification |
|---|---|
| `fb937d7` | Defined the complete acceptance path and independent ownership contracts before implementation. |
| `c39c2ef` | Strict central fleet registration, edit permissions, required checks, AI/delivery/release policy; negative schema and policy tests. |
| `9ee71b7` | SQLite tasks/events, idempotency, renewable fenced leases, repository exclusion, interruption, and unknown-outcome crash handling; real concurrency tests. |
| `5f6d4cb` | Separate Git clones, bounded file snapshots/edits, controlled commits, local publication and merge, GitHub PR/check/merge helpers; real temporary Git tests. |
| `0015681` | Real authenticated Codex structured diagnosis/repair/review, ephemeral read-only execution, disabled action tools, validation and request bounds; subprocess tests plus a live diagnosis. |
| `15806b6` | Loopback dashboard/API, same-origin mutation boundary, task dispatch, state/evidence display, and operator controls; HTTP boundary tests. |
| `0c08462` | Fixed stale Python bytecode reuse after same-size repairs; regression preserves source timestamp deliberately and proves new code executes. |
| `c7714d8` | Added fast-exit AI output-budget coverage and corrected formatting caught by hosted CI. |
| `113ddb0` | Connected baseline checks, AI repair/review, protected files, verified commits, Git delivery, merged-tree identity, deployment, health, and rollback; complete failure-path integration tests. |
| `43c55ca` | Added fleet/task/doctor/worker/serve/demo CLI, branch polling and scheduled checks, exact Codex version reference, and real three-repository demo; credential-free CLI subprocess tests. |
| `6197662` | Corrected dashboard hidden-panel behavior, readable times/state colors, and availability of operator controls; real Chromium inspection. |
| `19944fa` | Added a pinned Playwright storefront example with three real browser/API flows, managed server teardown, and fresh JUnit evidence. |
| `bcac998` | Made AI call budgets durable across worker epochs, hardened demo recovery handling, and removed an HTTP test upload/close race. |

Subsequent focused commits add the permanent dashboard browser regression, Windows/Linux browser CI, detailed configuration/runbook diagrams, and curated live acceptance evidence. Git history is the source of truth for those revisions.

## Verification and remaining boundaries

The complete quality profile runs lint, formatting, strict mypy, and deterministic pytest through Katydid itself. Real Chromium tests additionally exercise both the sample web application and the controller dashboard. Hosted CI runs both categories on Ubuntu 24.04 and Windows Server 2022 and preserves original evidence for seven days.

Live acceptance uses real Codex calls to repair seeded defects without changing protected tests, publishes/merges local Git candidates, verifies deployed application behavior, and recovers from a deliberately broken deployment. A separate real GitHub PR passed hosted Windows/Linux checks, merged into its fixture branch, and released the verified tree. Raw private-host evidence stays ignored; a curated revision/test/review summary is committed.

This release operates on trusted code on one host. Remote sandboxes, provider-specific reconciliation of unknown external effects, cross-repository compatibility campaigns, and red teaming remain future work. The full blueprint's proposed interfaces are not interchangeable with the implemented fleet/profile schemas.
