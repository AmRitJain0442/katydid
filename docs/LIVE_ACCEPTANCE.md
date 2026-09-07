# Live end-to-end acceptance

The implementation was exercised on 7 September 2026 using the existing private host login: Python 3.12.13, Codex CLI 0.153.4, model `gpt-5.6-sol`, high reasoning. These were real model calls, Git operations, test subprocesses, deployment commands, and health checks. Deterministic CI test doubles are recorded separately.

## Local fleet

The generated three-repository demo passed. The [machine-readable acceptance summary](evidence/live-acceptance.json) records task IDs, source/candidate revisions, test counts, AI reviews, and delivery outcomes without publishing private prompts or host logs.

| Repository | Baseline | Autonomous outcome |
|---|---|---|
| pricing | Five protected test cases failed | AI diagnosis, repair, fresh passing tests, separate AI approval, committed candidate, local publication/merge, deployed application healthy |
| catalog | Five protected test cases passed | Completed as healthy; no unnecessary AI call or source change |
| recovery | Five protected test cases failed | AI repair/review and local merge passed; a deliberately broken deployed artifact failed health; rollback restored the previous application and recovery health passed; task correctly marked failed |

Six real model calls handled diagnosis, repair, and independent review for the two failing repositories. The original protected verifier files remained byte-for-byte unchanged. Deployment in this reference adapter installs an executable Python application artifact into the registered local release directory; health imports that deployed artifact and asserts its behavior. This does not claim a cloud production deployment.

Original local evidence is in `.katydid/live-e2e/acceptance.json` and its `control/tasks/` directories. This directory is ignored by Git and was preserved.

## GitHub delivery

A separate fixture branch in this repository contained the seeded defect and protected tests. Katydid then used three additional real model calls to repair and review it, opened [PR #1](https://github.com/AmRitJain0442/katydid/pull/1), waited for hosted checks, and merged automatically. The fixture base was `katydid/e2e-base-20260907`; the platform's main branch was not the fixture's merge target.

- Baseline: `f6b711b70b04a16af5dde571173ea75386af5c2a`.
- Verified repair: `98dc98e6212617e183ac63f6b7c76662eb0c91e9`.
- GitHub merge: `c66318b13333f130026f1dd041301304bb2b4689`.
- [Hosted fixture checks](https://github.com/AmRitJain0442/katydid/actions/runs/34116766397): protected tests succeeded on Ubuntu 24.04 and Windows Server 2022.

The controller fetched the actual merged commit, verified that its tree matched the tested candidate, ran the registered local deployment command, and verified the deployed application was healthy. The task completed with the PR, candidate, merge, and health evidence retained in `.katydid/github-e2e/control/tasks/`.

## Reproduce

The localhost dashboard was also driven through real Chromium: enqueue, pause, steering, cancellation, and a second task completing through the worker all passed. The completed task contained five passing original tests, with no browser runtime errors. Visual inspection caught and fixed a hidden-panel CSS issue. The committed dashboard browser regression and storefront tests run in the browser CI jobs.

```text
python scripts/dev.py sync
python scripts/dev.py cli demo init .katydid/new-demo
python scripts/dev.py cli doctor --fleet .katydid/new-demo/fleet.yaml
python scripts/dev.py cli demo run .katydid/new-demo --live-ai
```

Use a new or empty directory. The live command requires authenticated Codex; it never falls back to a fake response. A successful demo requires actual repair, unchanged protected tests, successful delivery/health, and proven rollback after the intentionally failed release. See the [runbook](RUNBOOK.md) for the full workflow and [Git delivery guide](GIT.md) for registering a GitHub target under standing publication authority.

## Interpretation

This proves the supported single-host workflow on concrete fixtures. It does not establish exhaustive test coverage, guarantee that AI will fix every failure, prove untrusted-code containment, or certify a production deployment target. Registered requirements, tests, edit scope, and release/health/rollback commands define the operating contract. Red teaming is deferred.
