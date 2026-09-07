# Codex model provider

Katydid uses the real Codex CLI as a narrow model backend for diagnosis, repair proposals, and independent review. It does not contain a synthetic success response or silently fall back to a fake provider. A missing executable, missing authentication, backend error, invalid structured response, timeout, cancellation, or exhausted budget fails the task.

## Local authentication

Install the Codex CLI and run:

```console
codex login
codex login status
```

Complete the browser flow with the ChatGPT account and workspace that should fund and govern these runs. Katydid invokes `codex exec`, which reuses the CLI's saved authentication. A local operator using ChatGPT subscription access therefore does not need to configure an API key in Katydid. Codex also supports API-key authentication, but that is a separate usage-based path. See the official OpenAI documentation for [Codex authentication](https://learn.chatgpt.com/docs/auth) and [non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode).

The authentication cache grants model access and must stay on a trusted private host. Treat `~/.codex/auth.json`, when file storage is used, like a password. Do not copy live credentials into this repository, fixtures, logs, task evidence, containers, or ordinary CI secrets. The deterministic CI suite configures an explicit local fake executable and needs no network or OpenAI credential. Run the separately identified live-model test only on a trusted, authenticated host. OpenAI recommends short-lived workload identity or its dedicated action for suitable CI environments and warns against exposing a job-level key to repository-controlled code.

## Invocation boundary

Each request starts a fresh `codex exec` process with `--ephemeral`, `--ignore-user-config`, and an empty working directory. The sandbox is explicitly `read-only`. Shell, unified execution, apps, plugins, hooks, subagents, browser and computer use, host code mode, image generation, and image viewing are disabled. The prompt arrives on standard input; JSONL process events, standard error, the requested output schema, final structured response, and a validation receipt remain in the task evidence directory.

Codex receives data selected by the controller rather than general filesystem access:

- the central repository requirements and accumulated operator steering instructions;
- the configured `context_paths`, snapshotted as exact UTF-8 file contents, with the allowed `editable_paths` called out separately;
- baseline or candidate gate results and check metadata;
- at most the final 6,000 bytes from each captured check log;
- for repair and review, the candidate diff and actual verification evidence; and
- for diagnosis-informed repairs, the structured diagnosis. The independent review request omits the author's diagnosis.

The prompt labels this JSON as untrusted evidence. A model can only propose complete file contents in its structured response. The controller validates paths against central policy, applies the proposal itself, preserves protected files, runs the original checks, compares the resulting diff, and handles publication through separate policy-controlled code.

## Bounds and failure behavior

`AIConfig` sets the model, reasoning effort, command override, timeout, maximum calls per task, and maximum context bytes. Current validation permits 10–900 seconds, 2–20 calls, and 1,000–500,000 context bytes. The provider counts the JSON context's UTF-8 bytes before launch, caps the saved structured response at the same byte limit, rejects more than 10 MiB of combined CLI event and error output, and checks repair responses for 1–50 safe relative file edits. Cancellation terminates the active process tree.

These are local request, file, call, and elapsed-time bounds. They are not a hard token or monetary spend guarantee: a remote request can consume model work before a local timeout or output-size check stops the process. Account-side budgets, workspace policy, and provider usage controls remain necessary when a hard spend ceiling is required.

`ai.command` exists for deterministic tests and controlled launcher overrides. It is an argument array and is never passed through a shell. In normal operation, leave it unset so Katydid resolves `codex`; on npm-installed Windows launchers it resolves the underlying `codex.js` through `node` to preserve the same direct subprocess boundary.
