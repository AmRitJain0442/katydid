# Local execution and evidence

```text
python scripts/dev.py cli run path/to/quality.yaml
python scripts/dev.py cli run path/to/quality.yaml --stage nightly --output .katydid/nightly
```

The CLI validates and plans immediately before running. It checks that the profile source still matches the plan hash. It does not install application dependencies, start Docker, contact an AI model, clone other repositories, or merge/deploy anything.

## Execution

Checks run sequentially in manifest order. Ordinary failures do not prevent independent later checks from running. Commands use an argument array with `shell=False`, inherit the current environment, and receive no interactive stdin. `{python}` selects the Katydid interpreter; `{report}` supplies a fresh absolute JUnit path. `KATYDID_RUN_ID` and `KATYDID_REPORT_PATH` are also set for the child.

Each run receives an unpredictable unique directory. Each check receives its own fresh report path, so previous-run XML cannot accidentally satisfy a new check. Required test checks need both successful process exit and passing JUnit evidence. Partial skips are reported; an entirely skipped suite does not pass. The supported JUnit subset requires named testcases and consistent counters when supplied. Unsupported/malformed reports fail explicitly rather than being guessed into success.

## Output

The run directory is printed immediately to stderr. Final stdout is a JSON summary suitable for automation.

```text
.katydid/runs/<run-id>/
  profile.yaml             Snapshot of the profile used
  plan.json                Selected checks, exclusions, root and profile hash
  run.json                 Current state, outcomes, gate and runtime/source metadata
  000-unit/
    invocation.json        Expanded argv, working directory and timeout
    stdout.log             Original process output
    stderr.log             Original process errors
    junit.xml              Test runner's report, if produced
  cancel.request           Present only if cancellation was requested through the CLI
```

JSON checkpoints use atomic replacement. `running` and `cancelled` checkpoints cannot have a passing aggregate gate. Run state `completed` means execution finished; inspect `gate.passed` to determine success.

| Exit code | Meaning |
|---|---|
| 0 | Validation/plan succeeded, or execution completed and its required gate passed |
| 1 | Run completed with a failing gate |
| 2 | Invalid input or an execution/storage setup error |
| 130 | Run acknowledged cancellation |

Original reports/logs are local files, not tamper-proof attestations. Metadata records Git HEAD and dirty state when available, plus runtime and profile identity. This first adapter does not snapshot all repository contents or guarantee a dirty workspace can be reproduced; use a clean, unchanged checkout for reliable runs.

## Cancellation and timeouts

Press Ctrl+C in the executing CLI, or use another terminal:

```text
python scripts/dev.py cli cancel .katydid/runs/<run-id>
```

The second command requests cancellation; inspect the checkpoint for acknowledgment. It stops the current foreground process tree and marks remaining checks cancelled. SIGTERM is also handled where the operating system delivers it. A finished run is not retrospectively cancelled.

Checks poll every 50 ms. Timeout and cancellation termination use process groups on POSIX and `taskkill /T /F` on Windows, followed by bounded waiting. Cleanup failures appear in the result. The adapter does not undo application side effects and does not implement distributed pause/resume, durable leases, or guaranteed containment of detached children. Commands must remain in the foreground and wait for their own descendants.

## Limits and trust

- Trusted local repositories only: child commands have the user's filesystem/network access and inherited environment. The venv and working-directory checks are not a sandbox.
- Profiles are capped at 1 MiB and JUnit reads at 10 MiB; XML DTDs and entities are rejected.
- Combined process logs have a 10 MiB **soft** limit, checked at polling intervals. A burst can overshoot before termination; this is not an OS-enforced disk quota.
- Timeouts bound ordinary command execution; startup, cleanup, artifact writes, and filesystem stalls can add overhead.
- Do not edit source during a run. Git metadata is descriptive evidence, not a full source lock.
- A hard-killed runner can leave a `running` checkpoint and live resources. Its checkpoint stays nonpassing; crash recovery and independent sweeping are future work.
- Automatic retries, centralized mandatory policy, remote sandboxes, credential filtering, and trusted artifact signing are not implemented.

These limits are explicit acceptance boundaries for this first local adapter, not capabilities implied by the longer-term blueprint.
