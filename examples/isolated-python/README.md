# Isolated Python fixture

This dependency-free fixture runs a small allocation contract and a run-owned prepare/readiness/
cleanup lifecycle in a Linux container. Its profile pins the official Python 3.12.13 slim image by
digest and copies only `app.py`, `ops.py`, and `tests.py` into the read-only source snapshot.

The image must already exist locally. The runner never pulls or builds it:

```text
docker pull python@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36
docker image inspect python@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36
python scripts/dev.py cli run examples/isolated-python/quality.yaml
```

Opt into the real integration suite by setting the exact reference for that process. In
PowerShell:

```text
$env:KATYDID_DOCKER_IMAGE = "python@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36"
python scripts/dev.py test tests/test_docker_integration.py
Remove-Item Env:KATYDID_DOCKER_IMAGE
```

When the variable is absent, real Docker tests skip. When it is set, an invalid reference, missing
image, or unavailable daemon is a test failure. No test performs an implicit pull.
