# Repository profile v1

The implemented schema is deliberately smaller than the blueprint's proposed configuration. The loader rejects unknown fields, duplicate keys, anchors/aliases, unsafe YAML tags, invalid types, and files over 1 MiB. Loading or planning never executes commands.

```yaml
schema_version: 1
repository: example-service
owner: example-team
checks:
  - id: unit
    kind: test
    argv: ["{python}", "-m", "pytest", "--junitxml={report}"]
    working_directory: .
    timeout_seconds: 300
    stages: [pull-request, merge, nightly]
    required: true
```

The profile's parent is the repository root unless `--root` selects another directory. Working directories must exist and resolve inside that root, including through symlinks. Use forward slashes for portable relative paths. The root restriction prevents accidental directory escape; it does not sandbox the commands themselves.

## Fields

| Field | Rules |
|---|---|
| `schema_version` | Integer `1`; strings and booleans rejected |
| `repository` | Lowercase identifier, starts with a letter, at most 64 characters; letters, digits, hyphens |
| `owner` | Nonblank ownership label, at most 200 characters |
| `checks` | 1–100 checks with unique identifiers |
| `environment` | Optional ordered preparation, bounded readiness, and mandatory cleanup; see [Environments](ENVIRONMENTS.md) |
| `checks[].id` | Same identifier syntax as repository |
| `kind` | `command` for an exit-code check; `test` for mandatory JUnit evidence |
| `argv` | Nonempty list of strings; no implicit shell or environment expansion |
| `working_directory` | Default `.`; existing directory inside the root |
| `timeout_seconds` | Integer 1–3600; default 300 |
| `stages` | Nonempty unique list of `pull-request`, `merge`, `nightly`; default `pull-request` |
| `required` | Boolean; default true. Advisory results remain visible |

Every selected stage must include at least one required check. Excluded checks appear in the plan with the stage-selection reason. Plans contain the profile SHA-256 and resolved working directories and are deterministic for identical inputs.

```text
python scripts/dev.py cli validate path/to/quality.yaml
python scripts/dev.py cli plan path/to/quality.yaml --stage pull-request
```

`validate` checks both the profile and the selected plan. `plan` writes JSON to stdout. Invalid input exits 2 and writes a diagnostic to stderr.

## Execution contract

The runner uses `{python}` as an entire argv element for the current Katydid interpreter. `{report}` supplies a fresh per-check JUnit file path, `{run_id}` the current run identifier, and `{environment}` the unique data directory when a lifecycle is declared. `$VARIABLE`, pipes, and other shell syntax are not expanded. Direct `.bat`/`.cmd` executables are rejected because Windows can launch them through an implicit shell. Choose a native executable or explicitly invoke the intended shell instead. An explicitly invoked shell remains the user's command and is not made safe by this interface.

A `test` check must write JUnit containing actual test cases. A successful process with missing, malformed, empty, or entirely skipped test evidence does not pass. A `command` check uses its exit status and should represent builds/static commands, not hide test suites. Standalone runs enforce the profile's required checks; controller tasks additionally enforce registered [central fleet policy](FLEET.md).
