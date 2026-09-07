# End-to-end delivery plan

## Target

Complete a working, non-red-team path from registered repositories to durable tasks, real AI diagnosis/repair/review, verified Git changes, configured delivery, and observable human interruption. Preserve the existing local test CLI. Build and verify the whole path rather than stopping after independent modules.

The supported deployment is a single-host controller with a localhost dashboard, SQLite state, separate Git clones, and explicit repository/environment policies. It can manage multiple registered repositories through their existing command arrays. Cloud-specific deployment uses registered release/health/recovery commands; the reference demonstration uses a local deployment target. Untrusted-code hosting and a distributed multi-tenant service are not implied by separate Git clones.

## Concrete acceptance criteria

| Capability | Acceptance evidence |
|---|---|
| Multi-repository registration | Two independently registered Git repositories retain separate profiles, state, artifacts, and controls |
| Central policy | A repository or AI patch cannot weaken required checks, change allowed paths, or grant publication/release authority |
| Durable tasks | Queue survives restart; idempotent enqueue rejects conflicting payloads; leases fence stale workers |
| Interruption | Pause/cancel/steer accepted during execution and model calls; resume revalidates current evidence; no stale worker commits or publishes |
| Real AI backend | Authenticated Codex CLI returns validated structured diagnosis, file edits, and independent review in separate invocations |
| Autonomous repair | A real model fixes a seeded failure; original protected tests fail before and pass after; final review and wider tests pass |
| Git delivery | Candidate is committed in a separate clone; stale base/head rejected; local delivery and GitHub PR/merge boundaries exercised |
| Deployment | Exact candidate identity accompanies registered release command; health verification succeeds; a failed health check invokes configured recovery and records outcome |
| Dashboard/API | Register/select repository, enqueue, inspect stages/evidence, and interrupt tasks through real HTTP endpoints |
| Restart handling | Expired/incomplete work cannot appear complete or repeat unknown external side effects blindly |
| CI | Deterministic integration tests pass on Windows and Linux; model-free CI clearly separated from separately recorded live-model evidence |
| Operator experience | One documented setup and demo path; exact tools, configuration, credentials, artifact locations, and current execution boundaries |

## Implementation sequence and ownership

1. **Plan and contracts:** root defines configuration, task lifecycle, AI response contracts, and acceptance fixtures.
2. **Durable control:** Sol high agent implements SQLite tasks, events, epochs, leases, pause/resume/cancel/steer, and recovery tests.
3. **Workspace and Git delivery:** Sol high agent implements separate clones, constrained edits, exact-revision commits/publication, and integration tests.
4. **AI and central policy:** root implements strict fleet configuration, model adapter, request budgets, output validation, and diagnosis/repair/review prompts.
5. **Controller:** root connects leases, baseline execution, protected evidence, AI repair, independent review, publication, deployment, and recovery.
6. **Dashboard:** Sol high agent implements the localhost UI/API and request-boundary tests; root integrates controller dispatch.
7. **End-to-end verification:** root runs deterministic failure-path tests, a real model repair/review, configured delivery/health/recovery, and hosted CI; agents review integration gaps.
8. **Documentation and handoff:** exact commands and verified results, with implementation history and links to small commits.

Agents own separate files. Root reviews, integrates, commits, and pushes coherent slices. No agent may silently replace actual model or delivery evidence with a fixture result.

## AI contract

The model proposes data, never directly receives Git publication or deployment credentials. Calls use noninteractive Codex CLI with structured output and the existing local login. Inputs are explicitly selected source files, requirements, bounded logs, and result metadata. Fresh calls separate diagnosis/repair and review. The deterministic controller applies only allowed edits and enforces original required checks.

The model API is replaceable behind a narrow adapter. Live tests use the available authenticated Codex backend; CI uses explicitly named fake providers to exercise controller failures without credentials. [Official Codex noninteractive documentation](https://learn.chatgpt.com/docs/non-interactive-mode)

## Definition of done

All supported paths above are implemented, exercised together, documented, and pushed in microcommits. The live demonstration records source/candidate revisions, real model outcomes, test evidence, and delivery results. A missing live dependency or failed required integration remains unfinished work, not a green completion report.
