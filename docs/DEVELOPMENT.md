# Development environment

Katydid's first implementation is a local Python CLI. The long-term architecture is in the [blueprint](../AUTOMATED_TESTING_PLATFORM_BLUEPRINT.md); it is not a statement of implemented features.

## Exact toolchain

| Item | Source of truth | Purpose |
|---|---|---|
| Python 3.12.13 | `.python-version` | Project interpreter, installed by uv when needed |
| uv 0.12.10 | `.uv-version` and `tool.uv.required-version` | Environment and dependency management |
| Dependencies | `pyproject.toml` and committed `uv.lock` | Constraints and exact resolution |
| Virtual environment | `.venv/`, ignored by Git | All project dependencies; no global pip installation |
| Source | `src/katydid/` | Installable package and CLI |
| Generated run evidence | `.katydid/`, ignored by Git | Local reports and logs |

Windows and Linux are the initial validation targets. macOS support is not yet verified. Docker, a database server, GitHub credentials, and AI API keys are not required for local development.

## Setup and commands

Install Git, a bootstrap Python (3.10 or later is sufficient for the development script), and [uv](https://docs.astral.sh/uv/getting-started/installation/). Ensure `python` and `uvx` are on PATH. The helper invokes the pinned uv in its isolated tool cache, so an older global uv does not need to be replaced.

Run from the repository root in PowerShell or a POSIX shell:

```text
python scripts/dev.py sync
python scripts/dev.py cli --version
python scripts/dev.py lint
python scripts/dev.py format-check
python scripts/dev.py typecheck
```

The initial sync downloads the pinned interpreter and dependencies if absent. Subsequent commands use the locked `.venv`. No environment activation or `.env` file is necessary. `uv` separates the virtual environment and dependency lock from system Python. [uv projects](https://docs.astral.sh/uv/guides/projects/)

Use `python scripts/dev.py test` when tests are present. Use `python scripts/dev.py format` to format and `python scripts/dev.py build` to build a distribution. Additional arguments are forwarded as argument-array entries, never assembled into a shell command.

To deliberately update dependencies, use `uvx --from uv==0.12.10 uv lock --upgrade-package PACKAGE`, inspect the lockfile change, and run the relevant checks. Ordinary setup uses `--locked` and must not silently rewrite the lock.

## Environment boundaries

- This development environment is local. There are no cloud resources or deployments created by setup.
- Future local execution runs repository commands with the caller's permissions. A Python virtual environment is dependency isolation, not a security sandbox.
- Do not execute an untrusted repository in this local adapter. Remote sandbox and credential-broker support are separate milestones.
- Keep credentials out of manifests and committed files. Runtime evidence can contain application output; inspect it before sharing.
- Pin changes to Python or uv explicitly, regenerate the dependency lock when appropriate, and validate both CI operating systems.

## Microcommit workflow

1. Choose one reviewable capability or fix.
2. Implement the smallest coherent change and its meaningful failure-path tests.
3. Run the relevant tests and static checks.
4. Update the implementation log and user-facing instructions when behaviour changes.
5. Inspect the staged diff, commit with a concrete message, and push the microcommit.
6. Check CI for that revision. A failing CI run is unfinished work.

Keep the working tree clean between slices. Do not claim later blueprint capabilities are implemented by the CLI.
