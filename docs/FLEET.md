# Fleet policy reference

A fleet file is the operator-owned boundary between a registered repository and autonomous work. Repository code can describe how its checks run in `quality.yaml`; only the fleet file decides which repositories are accepted, which files the model may propose changing, which checks cannot be omitted, whether a candidate may be published or merged, and whether release commands may run.

The loader accepts strict YAML with a maximum size of 1 MiB. Unknown fields, duplicate keys, aliases, anchors, invalid types, and unsafe tags are rejected. Relative source and state paths resolve from the fleet file's directory.

Repositories may additionally declare an `isolation_policy` requiring Docker execution with an
approved image, source-file subset, namespace, and resource ceilings. See the complete
[isolation policy reference](ISOLATION.md#central-fleet-enforcement). This policy governs test and
environment-hook execution; registered delivery/release operations retain their trusted-host model.

## Complete example

```yaml
schema_version: 1
state_directory: .katydid/control

ai:
  provider: codex
  model: gpt-5.6-sol
  reasoning: high
  timeout_seconds: 180
  max_calls_per_task: 6
  max_context_bytes: 180000

repositories:
  - id: payments
    source: ../payments
    base_branch: main
    profile: quality.yaml
    context_paths:
      - src/payments.py
      - tests/test_payments.py
    editable_paths:
      - src/payments.py
    requirements: >-
      Preserve the public payment API. Amounts use integer minor units. Existing
      protected tests and all centrally required checks must pass.
    required_checks:
      unit: test
      lint: command
    repair_attempts: 2
    delivery:
      mode: local
      auto_merge: true
    release:
      auto_deploy: false
      deploy:
        id: deploy
        kind: command
        argv: ["{python}", "ops/release.py", "deploy", "{workspace}", "{commit}", "{release_dir}"]
        timeout_seconds: 300
      health:
        id: health
        kind: command
        argv: ["{python}", "ops/release.py", "health", "{commit}", "{release_dir}"]
        timeout_seconds: 120
      rollback:
        id: rollback
        kind: command
        argv: ["{python}", "ops/release.py", "rollback", "{commit}", "{release_dir}"]
        timeout_seconds: 300
```

Validate it without submitting work:

```text
python scripts/dev.py cli fleet path/to/fleet.yaml
python scripts/dev.py cli doctor --fleet path/to/fleet.yaml
```

## Top-level fields

| Field | Default and enforced meaning |
|---|---|
| `schema_version` | Required integer `1`. Booleans and strings do not coerce to it. |
| `state_directory` | `.katydid/control`. Holds `state.db`, task workspaces, run evidence, AI evidence, and release records. A relative value resolves beside the fleet file. |
| `ai` | Optional strict object selecting and bounding the AI provider. |
| `repositories` | One or more repository objects with unique IDs and unique Git sources. |

## AI fields

| Field | Default and rules |
|---|---|
| `provider` | `codex` by default; `gemini` selects the Vertex AI integration. |
| `model` | `gpt-5.6-sol` for Codex by default. Gemini requires an explicit model and `vertex_project`; see [Gemini configuration](GEMINI.md). |
| `reasoning` | `high`; one of `low`, `medium`, or `high`. |
| `command` | Normally omitted. An optional nonempty argument array for a controlled launcher override or deterministic test; no shell string is accepted. |
| `timeout_seconds` | `180`; integer from 10 through 900 for each model process. |
| `max_calls_per_task` | `6`; integer from 2 through 20 across diagnosis, repair, and review. The controller reserves calls durably before dispatch; interruptions, retries, and new worker epochs do not reset this task budget. A failed or interrupted dispatch still consumes its reservation. |
| `max_context_bytes` | `180000`; integer from 1,000 through 500,000. Bounds snapshotted source context, serialized model context, and the final structured response. |

Each model role runs ephemerally with execution tools disabled. It returns structured data;
the controller applies allowed edits and executes checks. See [Codex](AI.md) and
[Gemini](GEMINI.md) for their authentication and provider-specific limits.

## Repository fields

| Field | Default and rules |
|---|---|
| `id` | Required unique lowercase identifier: starts with a letter, uses letters, digits, and hyphens, and is at most 64 characters. This is the name used by task submission and discovery. |
| `source` | Required trusted local Git path or credential-free `https://github.com/...` URL. Relative local paths resolve beside the fleet file. SSH, other URL schemes, URL credentials, queries, and fragments are rejected. A resolved local path or case-insensitive GitHub source can be registered only once; a `.git` spelling variant is the same source. Combine its policy under one repository entry. |
| `base_branch` | `main`. Must be a safe Git branch name; the source head is captured when a task is enqueued. |
| `profile` | `quality.yaml`. Safe relative POSIX path inside the repository. It can never appear in `editable_paths`. |
| `context_paths` | Required list of 1–100 exact safe relative file paths. Existing regular UTF-8 files at these paths are the complete repository source snapshot given to the model; missing listed files are skipped. Globs and directory prefixes are not expanded. |
| `editable_paths` | Up to 50 exact paths, all also present in `context_paths`. Only these implementation files may be created or replaced by a proposal. An empty list allows testing but makes failed checks nonrepairable. |
| `requirements` | Required operator-owned text, 1–20,000 characters, supplied as standing requirements to every model role. |
| `required_checks` | Required nonempty map from check ID to `test` or `command`. Every named check must exist in each executed stage with the same kind and `required: true`. |
| `required_checks_by_stage` | Optional stage-to-check map adding mandatory checks; cannot change a common required check's kind. |
| `events` | Optional registered GitHub identity and PR/push/release event switches; see [Orchestration](ORCHESTRATION.md). |
| `repair_attempts` | `2`; integer from 1 through 5. Each attempt includes a proposal and actual verification; successful evidence then receives an independent model review. |
| `delivery` | Strict delivery policy described below. |
| `release` | Optional deploy, health, and rollback commands plus the `auto_deploy` discovery opt-in. It is valid only when delivery has `auto_merge: true`. |

Safe file paths are forward-slash relative paths without empty, `.`, `..`, `.git`, `.env...`, Windows drive/root, backslash, NUL, or alternate-stream components. Symlinks and reparse points are rejected during workspace access. The whole edit batch is validated before any file is replaced and is capped at 200,000 UTF-8 bytes.

`context_paths` separates information the model may read from general repository contents. `editable_paths` is a narrower implementation allowlist. Put tests, fixtures, interface definitions, and other useful evidence in `context_paths` while leaving them out of `editable_paths` to protect them. The controller snapshots protected context and rejects any change to it. It also rejects every Git change outside `editable_paths`, including changes produced by a check. The profile is always protected separately.

## Delivery fields

| Field | Meaning |
|---|---|
| `mode: none` | Default. The verified candidate is committed only in its retained task clone. `auto_merge` must be false. |
| `mode: local` | Push the candidate branch to the registered local source. With `auto_merge: true`, compare the expected base SHA and fast-forward the base branch. |
| `mode: github` | Requires `github_repository: owner/name`. Push the candidate, open a PR, and inspect that exact head through `gh`. With `auto_merge: true`, wait up to 900 seconds for reported checks and a clean merge state, then merge with an exact-head guard. |
| `github_repository` | Required for GitHub mode and rejected unless it is a simple `owner/name`. It must match the registered GitHub source when used. |
| `auto_merge` | `false`. Enables the exact-revision local or GitHub merge path. Katydid never requests admin bypass or force updates. |

GitHub mode relies on an already authenticated GitHub CLI and server-side repository rules. A clean PR with no reported checks becomes eligible after a ten-second grace period; configure required checks and branch protection on GitHub if a no-check PR must remain blocked. See [Git workspaces and delivery](GIT.md).

Immediately before delivery, the controller requires no remaining tracked diff after the candidate commit and reconstructs the binary diff from the registered base SHA to that commit. It must exactly equal the candidate diff that passed verification and model review. A mismatch stops delivery.

## Release hooks

For Gemini configuration and exact required GitHub check names, see the
[Gemini and GitHub guide](GEMINI.md). `delivery.github_required_checks` is central policy: every
named job must finish successfully before merge, including when GitHub has not yet created the jobs.

Every deployable AI repair now passes the profile's explicit `release` stage and central required
checks before publication. Missing or failed release evidence blocks publication. A manually
submitted `--stage release --mode release` task instead validates the current registered base and
runs the configured hooks without AI edits. Signed release events additionally require the explicit
`events.releases` opt-in. See [CI/CD orchestration](ORCHESTRATION.md).

`release.auto_deploy` defaults to `false` and accepts only a boolean. When true, ordinary watched
branch discovery may deploy a healthy new base commit after its mandatory release-stage profile
checks pass. An editable repository keeps one merge/repair task: a failing merge gate follows the
existing bounded repair path, while a healthy merge gate runs a fresh release-stage plan and then
the release hooks without an AI call. A repository with no editable paths receives a release/release
task directly. The discovered task key includes the exact head and policy digest, so repeated polls
return the same task rather than redeploying the same decision.

This opt-in applies only to ordinary changed-head discovery. Nightly discovery remains a nightly
check or repair, and manually submitted check-mode tasks remain check-only. Pull-request and push
webhook tasks are not promoted to deployment. A failed release-stage gate stops before deploy, and a
failed watched application gate cannot reach hooks unless the normal repair, verification, review,
merge, and release-stage gates all succeed. Changing the source head or central policy creates a new
decision; retrying a terminal release for the same head requires an explicit operator task.

`release.deploy`, `release.health`, and `release.rollback` are required `command` checks. Each uses the normal profile check fields: `id`, an argv array, optional `working_directory`, timeout, stages, and `required`. The fleet validator requires command kind and required status. Release execution substitutes `{workspace}`, `{commit}`, and `{release_dir}`; the normal runner also substitutes `{python}` and `{report}`.

After an exact tested tree is merged, Katydid runs deploy and then health. A passing health check records `releases/<repository>/last-success.json`. A failed deploy or health check invokes rollback followed by the same health hook. A healthy rollback leaves the task failed with recovery evidence. A failed or interrupted recovery leaves the task unresolved rather than asserting a known deployment state.

Hooks receive a fresh checkout of the released commit. Files generated during testing are not
deployment inputs. Before and after each hook, the controller checks the current base revision,
control authority, checkout HEAD, and tracked, untracked, and ignored files. Hooks must write
deployment state outside the checkout, normally under `{release_dir}`. A changed checkout or
superseding base revision stops the sequence; after external effects, this requires reconciliation.
Build artifact promotion is a subsequent increment.

The success pointer is atomically replaced while holding the store's write transaction and a valid
monitoring lease. This serializes the update with pause/cancel controls. The filesystem pointer and
SQLite event are separate durability domains: a host crash between them still requires
reconciliation. Remote branch movement and deployment are also not one atomic operation.

These commands run locally with the Katydid process's operating-system permissions. They are adapters for an environment the operator already controls; Katydid does not provision a container, cloud account, secret broker, or deployment platform.

## Repository profile and any test framework

The repository profile remains a separate, versioned file in the repository. Its `argv` arrays can invoke any installed framework or repository-owned adapter without shell interpolation. A `kind: test` check must write genuine JUnit XML to the fresh `{report}` path. If a framework cannot do that directly, register an adapter script that runs it and converts the real result:

```yaml
checks:
  - id: unit
    kind: test
    argv: ["{python}", "tools/run_tests_with_junit.py", "{report}"]
    timeout_seconds: 300
    stages: [pull-request, merge, nightly]
    required: true
```

Missing, malformed, empty, contradictory, or entirely skipped JUnit evidence cannot pass. `command` checks use the process exit status and suit lint, build, and static checks. The fleet's `required_checks` map overlays central enforcement so a repository change cannot rename, omit, make advisory, or change the kind of a required check. See [repository profile v1](PROFILE.md).

The broader product direction remains in the [automated testing platform blueprint](../AUTOMATED_TESTING_PLATFORM_BLUEPRINT.md). This fleet reference describes the implemented single-host, non-red-team path only.
