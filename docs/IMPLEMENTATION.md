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

Added the installable CLI, exact Python/uv selection, dependency lock, isolated development commands, and environment documentation. The CLI currently exposes help and version only. Validation: locked sync, CLI version, lint, formatting, and strict type checking.

### 2. Strict profiles and deterministic planning

Added the implemented v1 profile, command argument arrays, explicit stage selection/exclusions, source hashes, strict YAML/Pydantic validation, and resolved working-directory containment. `validate` and `plan` execute no repository code. Added negative tests for ambiguous configuration, invalid types, path escape, empty selections, and duplicate IDs. Windows cannot create the symlink fixture without additional privileges; Linux CI will exercise that case.

### Planned next slices

3. Honest test-evidence parsing and gate semantics.
4. Local runner with bounded execution, immutable run evidence, and CLI integration.
5. Windows/Linux CI, working examples, and final environment/run documentation.
