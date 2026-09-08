# Vultron operating platform rollout

The platform must be usable beyond its acceptance application. This increment
connects implemented capabilities to daily operation and replaces manual setup
gaps with reproducible commands. A passing fixture is evidence for a capability,
not a claim that every target repository has that capability enabled.

## Delivery slices

1. Put browser, unit, API, lint, and formatting checks into the registered
   application's primary pipeline, including dependency setup in each fresh clone.
2. Enable watched branch discovery and periodic checks with documented durable
   host startup and observable operational state.
3. Add repository onboarding that inspects existing project metadata and writes
   validated profiles and central registration without inventing test coverage.
4. Add real, bounded static, dependency, and secret scanning adapters with
   reproducible installation and failing findings represented as failing gates.
5. Wire applicable checks into CI and exercise them against real repositories.
6. Verify available container execution and configure supported isolation where
   the host supports it. External event ingress and application deployment require
   a concrete reachable endpoint and deployment target; do not represent local
   listeners or placeholder commands as deployed infrastructure.

Each slice receives focused validation and its own commit. Core/schema changes
also run the platform quality profile. Hosted checks must pass at delivered
revisions. Credentials remain host-owned and outside committed configuration.

Autonomous red-team campaigns remain outside this increment. Existing provider
selection stays explicit; enabling a second installed provider is not a reason
to silently fail over between accounts or models.
