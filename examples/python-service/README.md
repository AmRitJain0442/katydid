# Example repository profile

This is a tiny pricing module used as a trusted local test target. It does not start a web server, make payments, or access external services.

After syncing Katydid's development environment, run these commands from the Katydid repository root:

```text
python scripts/dev.py cli validate examples/python-service/quality.yaml
python scripts/dev.py cli plan examples/python-service/quality.yaml
python scripts/dev.py cli run examples/python-service/quality.yaml
```

The runner uses this profile's parent directory as the working root. pytest is available from Katydid's development environment. Real repositories must provide their own required runtimes and dependencies; the runner does not install them automatically.

Run artifacts appear in `examples/python-service/.katydid/runs/<run-id>/`. Read `run.json` for outcomes and the check's `junit.xml` for original test evidence. To see a failure, change a pricing expectation in a disposable copy and run that copy's profile. The process and aggregate gate should both fail.
