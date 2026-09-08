# Security checks

Katydid can run three local security gates through [`katydid.security`](../src/katydid/security.py):

| Gate | Pinned tool | Scope |
| --- | --- | --- |
| Static analysis | Semgrep CE 1.176.1 | Source patterns selected by an explicit repository-local rule file |
| Dependency vulnerabilities | Trivy 0.74.0 | Supported package and lock manifests among the approved repository files |
| Secret detection | Gitleaks 8.30.1 | Approved repository files using an explicit local configuration |

The adapter launches argument arrays without a shell, checks the exact scanner version, enforces an
internal timeout, caps combined console output at 2 MiB, caps private JSON at 10 MiB and findings at
5,000, and reconciles each tool's exit status with its parsed findings. Scanner JSON stays in a
temporary directory. JUnit evidence contains rule or advisory IDs, normalized repository-relative
paths, line numbers, and package names and versions. It never contains source snippets, Semgrep
metavariables, Gitleaks matches or secret values, vulnerability descriptions, or raw tool errors.

## Prepare the pinned tools

The setup supports Windows AMD64 and Linux x86_64. It creates an isolated tool directory; it does
not install a global package or edit the platform dependency lock:

```text
uvx --from uv==0.12.10 uv run --locked python scripts/security/setup.py prepare
```

That command performs these steps:

1. Syncs Semgrep 1.176.1 and all transitive packages from
   [`scripts/security/uv.lock`](../scripts/security/uv.lock) into
   `.katydid/security-tools/semgrep`.
2. Downloads the official Trivy 0.74.0 and Gitleaks 8.30.1 archive for the current platform.
3. Verifies the release archive before extraction. The expected SHA-256 values are
   `94c40e0696e4b907a74b7b2e1438d5d72ebaca83115817407f568a002d520842` for Trivy Windows,
   `2ae6fe3ee734b7fdf11335663e18c75ea12dccc76062f09f164a3b0f8be4371a` for Trivy Linux,
   `d29144deff3a68aa93ced33dddf84b7fdc26070add4aa0f4513094c8332afc4e` for Gitleaks
   Windows, and `551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb` for
   Gitleaks Linux.
4. Downloads the current Trivy vulnerability database into a new, uniquely named cache generation.
5. Records executable and database hashes, versions, paths, and refresh time in
   `.katydid/security-tools/setup.json`. This file contains no credential or scanner finding.

The scanner executables are reproducible. Vulnerability results are time-sensitive because the
advisory database changes. Refresh it deliberately before a security run:

```text
uvx --from uv==0.12.10 uv run --locked python scripts/security/setup.py refresh-db
```

Every refresh writes into a new cache generation and atomically publishes a replacement
`setup.json`. Previous generations remain in place so an in-flight scan keeps using the database it
validated. The scan itself disables Trivy database, Java database, check bundle, VEX repository,
version, and telemetry updates. It uses the recorded cache offline. This separates mutable advisory
ingestion from the evidence-producing check and makes the exact database hashes visible. Cleanup of
old generations is a separate operator action after no scans refer to them.

Setup needs HTTPS access to PyPI, GitHub Releases, and Trivy's default database registry. Scanner
execution does not install tools. Semgrep and Gitleaks run offline. Trivy runs with `--offline-scan`
against the prepared database. The setup script suppresses child output and never prints HTTP
headers, authentication, or raw failures. Use public release endpoints for this setup; do not place
registry credentials, tokens, or service-account files in the repository or tool directory.

## Run the example

[`examples/security-service/quality.yaml`](../examples/security-service/quality.yaml) declares all
three scanners as required `test` checks across pull-request, merge, nightly, and release stages.
After preparation, run its complete profile with:

```text
uvx --from uv==0.12.10 uv run --locked python scripts/security/run.py examples/security-service/quality.yaml
```

The runner validates the recorded executable hashes, passes the absolute manifest path through
`KATYDID_SECURITY_MANIFEST`, and invokes the normal Katydid runner. Evidence is written below the
repository's `.katydid/runs/` directory.

For another repository, copy the check entries, provide a reviewed local Semgrep ruleset and
Gitleaks configuration, and run:

```text
uvx --from uv==0.12.10 uv run --locked python scripts/security/run.py path/to/quality.yaml --root path/to/repository
```

Keep the rule and Gitleaks configuration files in the protected context set and outside AI-editable
paths. The adapter accepts only a relative scan root below the check's working directory and regular
local configuration files below that root. It builds a private temporary snapshot from Git-tracked
regular files and their current working-tree content. Git-ignored files are absent from the snapshot,
so host credentials, `.katydid`, `.venv`, and `node_modules` are never traversed. An unexpected,
nonignored untracked file fails the check instead of being silently omitted. A centrally reviewed
profile may repeat `--include relative/file` for an intended generated source file; includes still
must be regular, nonsymlink files inside the repository.

The adapter does not scan URLs, Git remotes, container registries, cloud accounts, or any path
selected by a model. Do not treat a clean scan as proof that a program is secure; the result covers
only the pinned tools, selected rules, recognized manifests, the tracked or explicitly approved
files in that checkout, and the recorded advisory database.

## Direct adapter commands

The profile wrapper is the normal interface. These equivalent commands show the underlying
contract; `{report}` is supplied by Katydid:

```text
python -m katydid.security static --root . --config security/semgrep.yaml --report {report} --timeout 120
python -m katydid.security dependencies --root . --report {report} --timeout 180 --max-db-age-hours 72
python -m katydid.security secrets --root . --config security/gitleaks.toml --report {report} --timeout 120
```

Missing tools, wrong versions, stale or absent Trivy cache data, malformed or oversized JSON,
scanner operational errors, timeout, and exit/result disagreement all produce an errored JUnit case
and a nonzero process exit. Findings produce failed JUnit cases and a distinct nonzero exit. A clean
gate requires a real scanner process, a parseable result, zero findings, a successful exit, and one
passing JUnit testcase.

`--max-db-age-hours` accepts 1 through 720 hours and fails closed when the published Trivy database
is older. A daily refresh with a 48-hour or 72-hour stage limit tolerates one failed refresh without
letting an indefinitely stale advisory snapshot pass.

Semgrep's official CLI documents local rules, strict mode, JSON, metrics control, and findings exit
behavior: <https://semgrep.dev/docs/cli-reference>. Trivy documents filesystem vulnerability scans
and the separate database lifecycle: <https://trivy.dev/docs/latest/target/filesystem/> and
<https://trivy.dev/docs/latest/guide/configuration/db/>. Gitleaks documents directory scans, JSON,
full redaction, and bounded scan flags: <https://github.com/gitleaks/gitleaks>.
