# Repository onboarding

`onboard` prepares a review bundle for an existing local Git repository or an HTTPS GitHub
repository. It inspects one committed revision, proposes or preserves a protected execution
profile, and writes a central fleet registration with validation-only authority.

Onboarding does not run setup code, dependency installation, tests, package scripts, environment
hooks, or containers. It does not edit the source repository, create a commit, configure a webhook,
grant application edit paths, enable delivery, or merge anything.

## Create a review bundle

Use an operator-owned destination outside the source repository. The destination must not exist;
Katydid never merges into or replaces an existing directory.

```text
python scripts/dev.py cli onboard C:/src/orders C:/vultron-config/orders-review \
  --repository-id orders --owner orders-team
```

For GitHub, use a credential-free HTTPS URL. Git may use the host's existing credential helper for
a private repository, but credentials cannot appear in the URL.

```text
python scripts/dev.py cli onboard https://github.com/OWNER/REPOSITORY.git \
  C:/vultron-config/repository-review --owner platform-team --base-branch main
```

If `--base-branch` is omitted, Katydid uses the local current branch or the remote default branch.
`--repository-id` is optional when a safe lowercase ID can be derived from the repository name.
The command prints JSON containing the inspected commit, generated paths, detected checks, exact
context paths, and unresolved gaps.

The destination contains:

- `quality.yaml`, the candidate repository-owned execution profile;
- `fleet.yaml`, the candidate operator-owned central registration; and
- `ONBOARDING_REVIEW.md`, the detection evidence, explicit context list, and gaps.

The fleet state path is `state`, resolved beside `fleet.yaml`. For a local repository, onboarding
rejects a destination inside the source so durable state and task clones cannot land in that source.

## What discovery reads

Local inspection reads Git objects from the selected local branch. Dirty and untracked working-tree
files do not affect the proposal. Remote inspection uses a shallow, single-branch bare clone, with no
working-tree checkout. The clone uses Git's blobless filter, so source blobs are fetched only when
needed for bounded metadata and context inspection. Both paths reject more than 20,000 committed
paths, cap individual metadata reads, and select at most 100 UTF-8 context files totaling 96 KiB.

The initial detector recognizes these existing conventions:

| Project evidence | Proposed command | Evidence contract |
|---|---|---|
| pytest in `pyproject.toml` | `{python} -m pytest -q --junitxml={report}` | Structured JUnit |
| Python `tests/` without pytest metadata | Katydid's isolated unittest adapter | Structured JUnit from unittest result callbacks |
| Ruff or mypy metadata/dependency | `{python} -m ruff check .` / `{python} -m mypy .` | Exit status and log |
| `package.json` test script plus Vitest dependency | direct native `node` invocation of Vitest with JUnit output | Structured JUnit |

Every newly proposed check is required at pull-request, merge, nightly, and release stages so an
enabled lane cannot silently omit the discovered baseline. Katydid records only commands supported
by committed evidence. If it finds no structured existing test command, onboarding fails and leaves
no bundle instead of inventing a passing check.

Discovery does not infer dependency installation, service startup/readiness, browser provisioning,
databases, cloud resources, secrets, Docker images, or release commands. These appear as explicit
gaps in the review. Cargo, Go, Jest, Playwright, and unknown package test scripts are not promoted to
`command` checks: doing so would hide a test suite behind exit status and could accept zero tests.
If no real structured test is available, onboarding fails and includes the unsupported reporter gap
in its diagnostic.

The unittest adapter runs through `python -I -m katydid.onboarding`, discovers the committed suite,
and translates actual success, failure, error, skip, expected-failure, unexpected-success, and
failing subtest callbacks into JUnit cases. An empty discovery writes a zero-case report and exits
nonzero, so the evidence gate cannot pass. The isolated Python launch prevents a source checkout
from shadowing the installed Katydid adapter module.

For unsupported frameworks, configure the project's actual runner to write JUnit containing real
cases at `{report}`, then add that reviewed direct argv as a `kind: test` check. On Windows, npm and
similar package-manager launchers resolve to batch files, so discovery does not emit them as portable
native argv. Do not add a wrapper that writes a passing report independently of the test process.

## Existing protected profiles

When the selected revision already contains `quality.yaml`, onboarding preserves its bytes. Loading
the profile validates its schema; environment hooks and isolation settings are not executed. The
central policy mirrors required checks shared by every stage the profile configures and adds its
stage-specific required checks. Stages with no required checks are listed as gaps and must remain
disabled. The current fleet schema needs at least one required check common to all configured
stages; if the profile lacks one, onboarding stops with an actionable error rather than weakening
the profile.

The review copy is evidence for comparison. The source profile remains authoritative and protected
from AI edits.

## Review and activate

For a generated profile, inspect every argv, working directory, stage, timeout, and listed gap. Copy
`quality.yaml` into the repository root only if there is no existing profile, review the diff, and
commit it through the repository's normal process. For an existing profile, confirm the review copy
still matches the committed source file.

Keep `fleet.yaml` in a central operator-owned directory. Before starting the worker:

```text
python scripts/dev.py cli validate C:/src/orders/quality.yaml
python scripts/dev.py cli plan C:/src/orders/quality.yaml --root C:/src/orders --stage pull-request
python scripts/dev.py cli fleet C:/vultron-config/orders-review/fleet.yaml
python scripts/dev.py cli doctor --fleet C:/vultron-config/orders-review/fleet.yaml
```

The generated fleet has:

- `editable_paths: []`;
- `delivery.mode: none` and `auto_merge: false`;
- no GitHub events, release hooks, or central isolation policy; and
- exact, bounded, nonsensitive committed `context_paths` rather than globs.

Run the first task explicitly in check mode:

```text
python scripts/dev.py cli task --fleet C:/vultron-config/orders-review/fleet.yaml \
  submit orders --stage pull-request --mode check
python scripts/dev.py cli worker --fleet C:/vultron-config/orders-review/fleet.yaml --once
```

A failing check remains failed evidence. It cannot become a repair because the generated central
policy grants no editable paths. Review the evidence and discovery gaps before changing authority.

To enable repair later, edit the central fleet file manually: replace the provisional requirements
with the application's authoritative behavior, add the smallest exact implementation files to both
`context_paths` and `editable_paths`, and review the model/provider budget. Delivery, GitHub event,
isolation, and release settings are separate grants described in [fleet configuration](FLEET.md).
No profile or model response can grant those central permissions.

Re-run onboarding into a new destination when the chosen source revision or discovery assumptions
change. Existing review directories are deliberately never updated in place.
