# Vultron agent workspace

The dashboard uses a three-column workspace: investigation history, the current
investigation, and a collapsible runtime and evidence inspector. Its layout takes
design reference from [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness),
particularly its app frame, central conversation column, and expandable process
rows. Vultron retains its own identity, original implementation, and red/black
palette. It does not depend on or run the DeepSeek harness.

## Using the workspace

1. Select **New investigation**, choose a registered repository, and **Enqueue task**.
   This runs the existing repository configuration; the UI grants no new permissions.
2. Search history by repository, task ID, stage, or state. **Active** includes queued,
   running, and paused tasks. **Attention** includes failed, unresolved, and paused tasks.
   History and the three recent investigations are ordered newest first.
3. Open **Activity** to follow the trajectory. Lease renewals and heartbeats are
   initially hidden; **Show system events** reveals them. Every event is inspectable.
4. Open **Checks** to compare baseline, repair/release verification, deployment,
   health, and rollback evidence when present. Failed baseline checks remain failed
   even if later verification passes. Expand a check for its report and log tails.
   Open **Live workflow** to watch individual tools during execution. The phase
   diagram shows stages reached and the current agent phase. Tool rows show pending,
   running, passed, failed, or interrupted status, with elapsed time. Expand a row
   for stdout and stderr; **Follow active tool** opens the next running tool. Output
   follows new text until you scroll back. Up to eight tool outputs can remain open.
5. Use **Pause**, **Resume**, **Cancel**, or a steering note on an active task.
   Completed tasks remain read-only. A rejected steering note stays in the composer.
6. Open the right inspector for the live provider, required checks, watch/schedule,
   delivery settings, task metadata, and raw JSON. **Download task record** exports
   the selected task and all its events, including hidden system events.

The inspector starts collapsed below 951px and can be reopened from the header.
At phone widths, panels stack and history has a bounded scroll area. Tabs support
arrow keys and Home/End. Focus indicators, a skip link, and reduced motion are supported.

## Implementation and evidence

All assets are local, with no frontend build step, remote fonts, or new dependencies.
The existing same-origin API and content security policy remain in effect. API text
is inserted through DOM text nodes. Polling runs every four seconds and preserves
search, selected view, expanded evidence, and unsent notes. Connection/runtime errors
are visible; a late task-list response cannot override a newer navigation.

`tests/browser/dashboard.spec.mjs` covers the real dashboard/worker control flow,
plus deterministic evidence, filtering, export, error, and viewport regressions.
Run the browser profile with:

```powershell
python scripts/dev.py cli run tests/browser/quality.yaml
```

The existing CI runs this profile on Windows and Linux. API boundary tests remain
in `tests/test_dashboard.py`. The redesign requires no worker restart because the
dashboard reads its static files on each request.

## Live workflow transport

The runner writes an atomic `progress.json` beside each check's logs, including
environment prepare/readiness/cleanup commands and release hooks. Start/finish
timestamps describe real execution; pending checks are read from the run plan.
The ordinary run checkpoint and gate remain authoritative. Failure to write the
optional progress file cannot prevent mandatory cleanup from executing.

`GET /api/tasks/{id}/live` lists runs in the task's current execution epoch.
Repeated `log` query parameters select keys returned by that endpoint. Reads never
use arbitrary client-supplied paths or task-result directory strings. Links,
junctions/reparse points, hard-linked files, other epochs, and path escapes are
excluded. Reads are bounded to sixteen recent runs, eight selected outputs, and
the latest 16 KiB per stream; truncation is visible. All data uses the dashboard's
existing loopback/same-origin boundary.

The active Live workflow tab polls every two seconds and suspends polling in a
hidden browser tab. Closed outputs are not transferred. Completed tasks retain
their evidence, while unfinished steps on terminal tasks are shown as interrupted
or not run. Output depends on when each tool flushes its streams. This includes
Playwright's console/reporter output; browser video is not streamed.

Each row names the executing tool separately from the check: for example
**Playwright** / Browser tests or **pytest** / API tests. Direct CLI invocations and
built-in security adapters are recognized automatically. Existing Orders records
also recognize their shipped wrappers. For custom wrappers, declare a display
name on the check (or environment/release hook):

```yaml
- id: browser
  kind: test
  tool: Playwright
  argv: ["node", "scripts/run-browser.mjs", "{report}"]
```

The name is descriptive metadata and does not change execution or gate behavior.
Unrecognized wrappers show their interpreter or executable until a tool is declared.

An existing service must be restarted once to load the live endpoint and runner
instrumentation. Older completed runs still expose their stored results/logs,
but have no new per-tool start/finish timestamps. The real browser acceptance test
asserts that subprocess output is visible while its task is still testing, then
observes the same row becoming passed without losing its expanded output.
