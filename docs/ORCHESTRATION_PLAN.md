# CI/CD orchestration increment

## Objective

Carry event intent and immutable revisions through GitHub ingress, durable tasks, selected checks,
and release decisions. Finish this increment before automatic onboarding and test generation.

## Acceptance

- Profiles and tasks distinguish pull-request, merge, nightly, and release stages. Existing manual
  repair tasks retain their default; event validation cannot silently invoke repair or deployment.
- Branch discovery selects merge checks and scheduled discovery independently selects nightly
  checks. Explicit release tasks validate the current approved revision before configured hooks.
- Central mandatory checks apply to every stage, with additional stage-specific requirements.
- Signed GitHub PR, push, and published-release events map only to registered repositories and
  permitted operations. PR event execution requires centrally enforced Docker isolation.
- Delivery receipts survive restart. Duplicate delivery has no repeated effect; conflicting reuse
  is rejected. New PR revisions atomically fence superseded work; paused work remains paused.
- Check execution records the actual source and base revisions. Stale refs, changed policy,
  cancellation, missing checks, and failed evidence cannot authorize success or release.
- A reusable GitHub workflow runs configured checks for two representative repositories across
  all four stages. It does not receive deployment credentials or perform deployment itself.
- Real Git/subprocess/HTTP tests cover signature failures, replay, concurrency, supersession,
  stage selection, failure, interruption, and release gating. AI paths retain existing regressions;
  model-free event validation remains available during model outages.
- Existing Windows/Linux core, browser, and Docker CI stays green, alongside hosted stage checks.

## Commit sequence

1. Record contracts and acceptance.
2. Add stage and task intent, exact revision execution, central gates, and controller regressions.
3. Add durable event receipts, supersession, and interruption preservation.
4. Add signed GitHub ingress and CLI operation with HTTP/event acceptance.
5. Add reusable CI workflows, two fixtures, operating diagrams, and hosted evidence.

## Boundaries

The listener binds to loopback; external GitHub delivery requires an operator-configured HTTPS
reverse proxy or tunnel. Secrets stay outside repository configuration. GitHub events cannot grant
new repository, execution, model, merge, or release authority. The container boundary remains
the documented Docker boundary. Automatic onboarding, new test authoring, artifact registries,
progressive production rollout, and Phase 2 hunting remain subsequent increments.

## Subsequent sequence

1. Automatic repository onboarding and independently validated test generation.
2. Component-aware planning and validated specialist test integrations.
3. Multi-service environments and scoped temporary identities.
4. Cross-repository compatibility and system journeys.
5. Immutable artifact delivery, production monitoring, and recovery reconciliation.
6. Firm operations, broader interruption controls, reporting, and measured efficiency.
7. Held-out AI evaluations and the separately scoped Phase 2 hunting capability.

Each increment needs its own concrete acceptance contract, small commits, and verified integrations.
