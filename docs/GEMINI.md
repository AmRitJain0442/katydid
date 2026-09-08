# Gemini through Vertex AI

Katydid can use Gemini for the same diagnosis, repair proposal, and independent review roles as
the Codex provider. Select the provider explicitly in the central fleet configuration:

```yaml
ai:
  provider: gemini
  model: gemini-2.5-flash
  vertex_project: your-google-cloud-project
  vertex_location: global
  timeout_seconds: 180
  max_calls_per_task: 6
  max_context_bytes: 180000
  max_output_bytes: 200000
  max_output_tokens: 8192
```

The model must be explicitly configured and available to the project. Gemini does not accept
the Codex launcher override. There is no automatic switch to another provider or test double.

## Authentication and operation

Set the application credential path in the PowerShell session that runs the controller:

```powershell
$env:GOOGLE_APPLICATION_CREDENTIALS = "C:\private\katydid-gemini.json"
python scripts/dev.py sync
python scripts/dev.py cli doctor --fleet path/to/fleet.yaml
python scripts/dev.py cli task --fleet path/to/fleet.yaml submit orders --stage nightly --mode repair
python scripts/dev.py cli worker --fleet path/to/fleet.yaml --once
```

Use a project with Vertex AI enabled and an identity authorized for model inference. The
credential belongs on the controller host. It is not a repository file, CI secret, model prompt,
or task artifact. The provider does not need a Gemini API key for this Vertex AI path. Doctor
checks authentication and inference with a small, real structured-output request. It consumes
provider usage. Gemini uses the model's default thinking behavior for task calls; the shared
`reasoning` option configures Codex and does not set Gemini's thinking budget.

Repository checks and release hooks do not inherit `GOOGLE_APPLICATION_CREDENTIALS`,
`GEMINI_API_KEY`, or `GOOGLE_API_KEY`. Local subprocesses still share the trusted host and user;
environment filtering is not filesystem isolation. Use the documented Docker boundary for
centrally approved container execution.

## Evidence and control

Each role runs in a fresh, killable model subprocess. The SDK receives selected JSON evidence,
an output schema, and no execution tools. Katydid validates the response before using it, applies
only centrally permitted edits, reruns protected tests, and independently reviews the resulting
diff and verification evidence. The SDK never directly edits or publishes repository files.

Task-local receipts record the provider, model, role, duration, validation result, and available
usage/request metadata. Failure diagnostics use bounded categories without raw authentication
exceptions. Context size, calls, response bytes, generated tokens, and elapsed time are bounded.
Stopping the local subprocess cannot guarantee cancellation of work already accepted remotely;
call and token limits are not a monetary budget guarantee.

## Required GitHub checks

GitHub delivery can name the exact check contexts required before merge:

```yaml
delivery:
  mode: github
  github_repository: your-owner/your-repository
  auto_merge: true
  github_required_checks:
    - Core (ubuntu-24.04)
    - Core (windows-2022)
    - Browser
```

Use the actual job/check names from the target repository. Missing, pending, skipped, and neutral
required jobs cannot satisfy this gate. Every named check must succeed on the expected PR head;
an additional failing status still blocks delivery. This is controller policy and does not install
GitHub branch protection or bypass existing branch rules.

## Provider references

- [Google Gen AI Python SDK](https://github.com/googleapis/python-genai)
- [Vertex AI quickstart](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/start/quickstart)
- [SDK structured JSON output](https://googleapis.github.io/python-genai/)

The separate [GitHub acceptance plan](GEMINI_GITHUB_PLAN.md) distinguishes deterministic tests
from real model calls and a real pull-request delivery.
