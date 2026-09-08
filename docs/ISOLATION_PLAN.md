# Container execution and abandoned-resource cleanup

This increment extends the local environment lifecycle before autonomous adversarial campaigns.

## Acceptance contract

- An optional strict `isolation` profile selects Linux Docker execution. Existing local profiles
  keep their current trusted-host behavior; requested container execution never falls back locally.
- Only explicitly listed regular source files enter a fresh read-only snapshot. Git metadata,
  credential files, links, and paths outside the source root are rejected. Images must be pinned
  by digest and already installed; dependency installation/image building is an operator step.
- Every check, including preparation/readiness/cleanup, executes as an unprivileged user with
  no external network, no host credentials, dropped capabilities, no-new-privileges, a read-only
  root filesystem, and bounded CPU/memory/process/tmpfs settings. No arbitrary Docker flags,
  privileged mode, socket mounts, or network overrides are accepted.
- A check can write only its own report output, the run's environment data, and bounded tmpfs.
  Runner checkpoints and previous check evidence are outside its writable mounts. Source snapshots
  are read-only. These containers share the Docker host kernel and are not a VM security boundary.
- Container lifecycle metadata includes immutable resource IDs, ownership labels, and a fixed
  expiry. Normal completion, cancellation, and timeout remove the container and verify removal.
  Docker/setup/collection/removal failures block the gate and application AI repair.
- An independent namespace-scoped sweeper removes only expired, correctly labelled Katydid
  containers. It supports one-shot and long-running operation, is idempotent, and does not remove
  unlabelled, malformed, other-namespace, or unexpired resources. No host directory pruning.
- Central fleet policy can require Docker isolation and approved images/source paths. A repository
  cannot silently downgrade a configured isolation requirement.
- Real Docker tests prove source/report separation, credential absence, outbound-network denial,
  non-root/resource constraints, cleanup, and recovery after killing a runner. Deterministic tests
  cover error paths without requiring Docker on every developer host.
- Local Linux containers on Docker Desktop and dedicated Ubuntu container CI exercise the same
  adapter. Existing Windows/Linux core and Chromium jobs must remain green.

## Explicit remaining boundaries

This is an offline single-container-per-command adapter. An application and its network test client
must run within the same command/container. Shared multi-container networks, cloud resources,
short-lived external credential injection, hostile-code VM isolation, disk quotas for retained host
evidence, and automatic security campaigns remain later increments. Sweeping requires an independent
live process and an available Docker daemon; it cannot remove resources while that daemon is down.

## Microcommit sequence

1. Record the execution and recovery contract.
2. Implement strict profiles, container execution, runner integration, and deterministic tests.
3. Add scoped sweeping and real crash/containment acceptance.
4. Enforce fleet isolation policy and controller failure handling.
5. Add runnable fixtures, CI, and operating documentation with actual acceptance evidence.

## Local acceptance — 2026-09-08

- Full Katydid quality run `f85dfb67ee0e4cba84c13e7b61527d54` passed lint, formatting,
  strict mypy, and pytest: **322 passed, 10 skipped** in 118.86 seconds. Five skips are the
  separately invoked Docker suite; five require unavailable Windows symlink/FIFO capabilities.
- The opt-in real Docker suite passed **all five tests** in 25.63 seconds on Windows 11 with
  Docker Desktop's Linux engine 27.5.1 and the example's immutable Python image. It exercises
  containment, fresh repaired-source verification, cancellation, detached-child timeout cleanup,
  and independent sweeping after a runner is forcibly terminated, including unrelated decoys.
- The controller acceptance uses a deterministic AI provider with real Git and Docker operations.
  It proves the isolation integration around the existing AI interface; it is not a new live model
  acceptance. Earlier real Codex acceptance remains documented in [LIVE_ACCEPTANCE.md](LIVE_ACCEPTANCE.md).
- Example run `28c39f3381e44be6a65cf8016989ede8` passed all four business/containment cases and
  preparation/readiness/cleanup, with all four owned containers removed. No managed containers
  remained after acceptance. Both source distribution and wheel built successfully.
- Microcommits `cec0dd0`, `5bcbd66`, and `7ce17ce` implement the container/policy boundary,
  failure handling and sweeping, and real acceptance/example/CI respectively. Hosted CI adds a
  dedicated Ubuntu Docker job alongside both existing core and browser operating-system jobs.

Raw run evidence stays in ignored `.katydid` directories. An independent sweeper must be operated
as a separate live process; these tests do not install an operating-system startup service.

The first hosted run exercised all 327 Linux core tests successfully and exposed two portability
issues: mypy needed a recognized platform guard around the Windows-only launch flag, and the
example's atomic nonsecret cleanup record needed explicit `0644` permissions so the host's different
UID could read it. Both were corrected; Windows/Linux type checks and all five local real Docker
tests passed again, with POSIX evidence readability now asserted by the integration suite.
