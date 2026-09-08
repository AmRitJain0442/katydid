# Gemini and GitHub acceptance increment

Connect a real Gemini provider to the existing bounded diagnosis, repair, and independent review
interface, then exercise it against a separate runnable GitHub application.

## Acceptance

- Codex remains the default. Explicit Gemini configuration selects Vertex AI, project, location,
  and model, with host-only application credentials and no silent provider fallback.
- Each model call has enforced time, context, call, output, and generated-token bounds. Gemini
  receives selected evidence and returns validated JSON proposals without execution tools.
- Cancellation stops the local model subprocess. Receipts retain model identity and usage without
  credential contents. Doctor verifies the selected authentication mechanism without requiring Codex
  for Gemini fleets. Remote work already accepted by a provider may still consume usage.
- A separate private GitHub repository contains a usable SQLite order application, a browser UI,
  authoritative requirements, protected unit/API/browser tests, and pinned CI configuration.
- One disclosed pricing defect makes the initial tests fail. A real Gemini run repairs only the
  centrally permitted implementation file. Protected tests and requirements remain unchanged.
- Katydid creates a real GitHub pull request, waits for checks on the exact candidate head, and
  merges only a verified candidate. The runnable application and original test evidence remain
  available for inspection. No cloud application deployment is implied by GitHub delivery.
- Deterministic provider tests exercise invalid output, errors, cancellation, and budgets without
  live credentials. Live Gemini acceptance is recorded separately with task, PR, and commit IDs.

## Commit sequence

1. Record the contract and exclude local service-account credentials.
2. Add the provider, strict configuration, dependency lock, and deterministic regressions.
3. Integrate provider selection and provider-aware authentication diagnostics.
4. Commit the separate application and CI in small slices; publish its initial failing contract.
5. Run the real repair/PR/checks/merge sequence and record curated, credential-free evidence.

This increment adds a model backend and a realistic acceptance application. Automatic repository
onboarding, general test generation, cloud deployment, and red teaming remain separately tracked.
