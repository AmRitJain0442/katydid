# Managed local releases

## Unattended Windows recovery

After the first successful deployment, register recovery under the same host account:

```powershell
powershell -NoProfile -File scripts/install_release.ps1 -State C:/vultron/state/releases/orders -Spec C:/vultron/config/orders.json -Logs C:/vultron/logs -TaskName Vultron-Orders-Recovery
Start-ScheduledTask -TaskName Vultron-Orders-Recovery
```

This task checks the established release at logon and every minute. It restores a
verified crashed application using the immutable artifact and existing data, without
needing its old Git checkout. Explicitly stopped releases remain stopped. Failed
candidate deployments remain subject to the controller's rollback flow. The task
runs while the configured user is logged on; deploy a dedicated service account on
an always-on host for continuous service. Inspect `LastTaskResult` and the bounded
`release-recovery.log` for failures. On Linux, invoke `scripts/maintain_release.py`
with the same `--state`, `--spec`, and `--logs` through a one-minute systemd timer.

`katydid.deployment` runs one loopback service from an exact Git commit while keeping mutable data outside the checkout. It is intended for a Katydid fleet release hook on a Windows or Linux host. It does not deploy to cloud infrastructure or adopt an existing process.

The adapter performs a fixed-port cutover in this order:

1. Require the release workspace to have the requested commit at `HEAD` and no tracked, untracked, or ignored changes.
2. Materialize the commit with `git archive` into `STATE/releases/COMMIT`, reject links and submodules, record a SHA-256 manifest, and make the tree read-only where the host supports it.
3. Verify and stop the recorded service, preserving `STATE/data`.
4. Start the candidate with an argv array and no shell.
5. Accept the candidate only when its loopback health response identifies the requested commit.

This fixed-port design has a short restart window. The previous service must release the port before the candidate starts.

## Host-owned deployment specification

Store the specification outside the application checkout. For OrderLab on port 8790, use:

```json
{
  "schema_version": 1,
  "argv": [
    "{python}",
    "-m",
    "orderlab.app",
    "--host",
    "127.0.0.1",
    "--port",
    "8790",
    "--db",
    "{data}/orders.db"
  ],
  "working_directory": ".",
  "environment": {},
  "health": {
    "url": "http://127.0.0.1:8790/health",
    "expected_headers": {
      "X-Katydid-Revision": "{commit}"
    },
    "timeout_seconds": 20,
    "interval_seconds": 0.2,
    "request_timeout_seconds": 2
  },
  "stop_timeout_seconds": 10
}
```

The supported placeholders are:

| Placeholder | Value |
| --- | --- |
| `{commit}` | Requested full Git object ID |
| `{release}` | Verified immutable release directory |
| `{data}` | Persistent `STATE/data` directory |
| `{python}` | Python interpreter running the adapter |

Placeholders may appear in `argv`, environment values, the health URL, expected response body, and expected response headers. An expected response body or header must contain `{commit}` so that a stale or unrelated listener cannot satisfy the probe. Health URLs must use explicit loopback HTTP with a port.

Only a small set of operating-system variables such as `PATH`, the user profile, temporary directories, and locale are inherited. Put application settings such as `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION`, `GOOGLE_GENAI_USE_VERTEXAI`, or `GOOGLE_APPLICATION_CREDENTIALS` in the host-owned `environment` object when needed. Katydid sets `KATYDID_DATA_DIR` and `KATYDID_RELEASE_COMMIT`; these and its other runtime variables are reserved.

## Fleet release hooks

Use the fleet controller's `{workspace}`, `{commit}`, and `{release_dir}` substitutions. Here `/absolute/host/orderlab-deployment.json` is the host-owned specification:

```yaml
release:
  deploy:
    id: managed-deploy
    kind: command
    argv: ["{python}", "-m", "katydid.deployment", "deploy", "--workspace", "{workspace}", "--commit", "{commit}", "--state", "{release_dir}", "--spec", "/absolute/host/orderlab-deployment.json"]
    timeout_seconds: 120
  health:
    id: managed-health
    kind: command
    argv: ["{python}", "-m", "katydid.deployment", "health", "--workspace", "{workspace}", "--commit", "{commit}", "--state", "{release_dir}", "--spec", "/absolute/host/orderlab-deployment.json"]
    timeout_seconds: 60
  rollback:
    id: managed-rollback
    kind: command
    argv: ["{python}", "-m", "katydid.deployment", "rollback", "--workspace", "{workspace}", "--commit", "{commit}", "--state", "{release_dir}", "--spec", "/absolute/host/orderlab-deployment.json"]
    timeout_seconds: 120
```

The rollback hook receives the failed candidate commit. It starts the recorded previous artifact with the same host specification and persistent data. The following health hook still receives the candidate commit but reports and probes the restored `active_commit`. A first deployment has no rollback target.

The optional operator stop command uses the current release operation commit:

```text
python -m katydid.deployment stop --workspace WORKSPACE --commit COMMIT --state STATE --spec SPEC
```

Every successful command prints one JSON object. Diagnostics go to stderr and return a nonzero status.

The bounded recovery command needs no Git checkout:

```text
python -m katydid.deployment recover --state ABSOLUTE_STATE --spec ABSOLUTE_SPEC
```

Register it with the host service manager or scheduler at login/startup and at a suitable bounded interval, such as one minute. It acts only when the registry describes a previously healthy deployment or successful rollback. It verifies the unchanged host spec and immutable active artifact, returns healthy without restarting a live healthy service, and restarts a dead or unhealthy service. It returns a successful `skipped` result for stopped, uninitialized, deploying, or failed release states, so an explicit stop or failed candidate is not resurrected.

A recorded host boot identity lets recovery discard stale pre-reboot process records without signaling a recycled PID. A same-boot PID identity mismatch still fails closed. Recovery preserves `operation_commit`, `previous_commit`, rollback metadata, and the established registry status. The supervisor terminates after the application exits; the next scheduled recovery invocation performs the restart.

## Persistent data and initial migration

`STATE/data` is never copied into a release. Application stdout and stderr are written under `STATE/logs`; manifests, release trees, launch handshakes, and the active-process registry remain under `STATE` as host-owned operational records.

For an existing SQLite service, stop the old writer and use the SQLite backup API to create `STATE/data/orders.db`. Do not copy a live database file. Take a separate backup before the first managed deployment. Later deployment and rollback operations reuse that database; schema changes must remain backward-compatible with the rollback release.

## Ownership and recovery

The registry records both the process ID and OS birth identity for the supervisor and application. Linux uses `/proc` start ticks and executable identity. Windows uses process creation time and executable identity. Stop and forced cleanup re-read this identity before signaling. If a PID has been reused or identity cannot be proved, the adapter fails without signaling it.

The supervisor accepts its random stop token only on a private loopback control socket. The token is stored only in host-owned state and is not placed in command-line arguments or application environment. Protect the entire state directory with host account permissions.

If the fixed port is already held on first deployment, startup and health fail and the adapter does not adopt or kill that listener. Stop the known legacy service through its existing supervisor before retrying. A changed deployment specification, edited registry, mutated release tree, or process identity mismatch also fails closed. Preserve the state directory and reconcile the actual listener and recorded PIDs as the service account; do not guess a PID or replace registry identity fields.
