# Environment lifecycle increment

This is the next sequential improvement from the blueprint audit (sections 7, 11, 15, 17). Complete and validate this increment before expanding another capability.

## Contract

An optional profile `environment` declares ordered `prepare` command checks, optional bounded `readiness`, and mandatory ordered `cleanup` commands. Commands execute on the trusted local host. Every lifecycle check must be required and of kind `command`; its `stages` field is prohibited because the lifecycle applies to the selected run as a whole.

Preparation and tests receive a unique `{environment}` directory and `{run_id}`. These are also exposed as `KATYDID_ENVIRONMENT_DIR` and `KATYDID_RUN_ID`. Preparation may install locked dependencies, migrate and seed run-owned data. Readiness retries only its explicit probe, retaining each attempt. Tests start only after preparation and readiness succeed.

Cleanup runs in a finally path after setup begins, including partial setup, test failure, timeout, cancellation and unexpected exceptions. It uses its own bounded command deadlines so the cancellation that stopped test work cannot suppress resource disposal. All cleanup commands are attempted, even if an earlier cleanup command fails. Cleanup must be idempotent and act only on resources owned by this run. No test result may report overall success before cleanup completes successfully.

Run checkpoints retain lifecycle outcomes and original command evidence. The fleet controller classifies preparation/readiness/cleanup failures as infrastructure failures and stops application repair. The service waits for the worker's configured cleanup on graceful shutdown.

## Acceptance

- Legacy profiles continue to work unchanged.
- Invalid lifecycle kinds, optional hooks, stage filters, duplicate IDs and path escapes fail validation.
- Real subprocess tests prove ordering, fresh data isolation, readiness retries and deadline expiry.
- Failed/partial preparation never launches tests and still invokes cleanup.
- Test failures and cancellation preserve their original outcome while cleanup runs.
- Cleanup failure blocks a passing test suite; later cleanup commands still run.
- Unexpected worker exceptions cannot skip the cleanup finally path.
- A real SQLite fixture migrates/seeds a separate database per run, executes meaningful assertions, and removes its database afterward.
- Controller integration proves lifecycle failures do not invoke AI repair and baseline/verification receive separate environments.
- Existing Windows/Linux quality and browser CI remain green; the SQLite example runs in both core CI jobs.

## Remaining environment work

This increment does not supply Docker/Testcontainers, network or credential isolation, cloud/device provisioning, a resource registry, or an independent expiry sweeper. A hard-killed host cannot execute its finally block. Such runs remain nonpassing and require cleanup from retained evidence until provider-specific reconciliation and sweeping are implemented. The blueprint's complete environment capability remains partial.
