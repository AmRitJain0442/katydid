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
