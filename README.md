# Katydid

Automated testing, CI/CD, and autonomous AI bug hunting.

A platform in development for adaptable repository testing, with a blueprint for autonomous bug hunting, verification, repair, review, and delivery.

**Status:** First working local execution slice. Profile validation, planning, process execution, JUnit evidence, aggregate gates, and cooperative cancellation are implemented. The full autonomous platform and remote sandbox are not yet implemented.

## Start here

See the [development environment](docs/DEVELOPMENT.md) for exact tool versions and setup commands, and the [implementation record](docs/IMPLEMENTATION.md) for completed slices and next steps.

```text
python scripts/dev.py sync
python scripts/dev.py cli run examples/python-service/quality.yaml
python scripts/dev.py cli run katydid.yaml
```

The example runs a small local test suite. Katydid's own profile runs lint, format checks, strict type checks, and the platform tests. No Docker daemon, cloud credentials, or model API keys are required.

Read the [implemented profile schema](docs/PROFILE.md) and [execution/evidence guide](docs/RUNS.md) for command semantics, exit codes, cancellation, and current limits. This first adapter executes **trusted local code with your permissions**, not sandboxed untrusted repositories.

Read the [Automated Testing Platform Blueprint](AUTOMATED_TESTING_PLATFORM_BLUEPRINT.md) for the complete architecture, tool selections, execution steps, Mermaid diagrams, and implementation acceptance criteria.

The blueprint covers:

- Repository onboarding and adapters for web applications, services, libraries, mobile/desktop applications, data pipelines, infrastructure, and specialist workloads.
- Pull-request testing, cross-repository compatibility, isolated environments, and reproducible test data.
- CI/CD gates, immutable release artifacts, progressive deployment, monitoring, and recovery.
- A Phase 2 AI team for functional and security bug hunting, independent verification, regression tests, and repairs.
- Autonomous operation with independent AI review, policy-controlled merges and releases, and human pause, cancel, steering, and takeover controls.
- Evidence, uncertainty, flaky tests, execution budgets, ownership, and staged adoption.

## Operating model

AI handles routine work within standing policies. Humans can inspect and interrupt at any point; ordinary tasks do not wait in human approval queues. Independent execution evidence and deterministic gates govern consequential actions.

Missing evidence and unresolved intent remain explicit. Autonomous operation does not imply exhaustive coverage or permission to expand its own scope.

## Roadmap

| Phase | Outcome |
|---|---|
| 0 | Repository inventory, pilot definition, and standing execution scope |
| 1 | Automated testing foundation, isolated environments, reporting, and interruption controls |
| 2 | Automated AI bug hunting, red teaming, verification, repair, and eligible merging |
| 3 | Cross-repository compatibility and coordinated campaigns |
| 4 | Release integration, autonomous deployment, monitoring, and recovery |
| 5 | Additional repository profiles and specialists |
| 6 | Measured improvements to testing and AI hunting effectiveness |

Tool capabilities are linked to official documentation in the blueprint. Its larger configuration examples remain proposed interfaces; `katydid.yaml` and `examples/python-service/quality.yaml` use the implemented local schema.
