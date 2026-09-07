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

## Next milestone

Add centrally enforced policy independent of repository declarations, a durable run/control store with crash reconciliation, and supervised environment adapters before executing untrusted workloads or adding AI-driven side effects. Preserve this local adapter as the reproducible execution contract. AI coordination should consume proven plans/evidence rather than replace the gate semantics.
