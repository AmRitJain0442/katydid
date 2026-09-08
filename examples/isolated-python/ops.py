"""Run-owned environment hooks for the dependency-free container fixture."""

import json
import os
import sys
import tempfile
from pathlib import Path

STATE = "state.json"
READY = "ready.json"
TESTED = "tested.json"
CLEAN = "cleanup.json"


def environment(argument: str) -> tuple[Path, str]:
    configured = os.environ.get("KATYDID_ENVIRONMENT_DIR")
    run_id = os.environ.get("KATYDID_RUN_ID")
    if not configured or not run_id:
        raise RuntimeError("Katydid environment variables are required")
    target = Path(argument).resolve(strict=True)
    if target != Path(configured).resolve(strict=True) or not target.is_dir():
        raise RuntimeError("environment path does not match the run-owned directory")
    return target, run_id


def read_owned(path: Path, run_id: str) -> dict[str, object]:
    raw: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
        raise RuntimeError("environment data is not an object with string keys")
    value: dict[str, object] = dict(raw)
    if value.get("run_id") != run_id:
        raise RuntimeError("environment data belongs to another run")
    return value


def write_atomic(path: Path, value: dict[str, object]) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".isolated-python-",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def prepare(root: Path, run_id: str) -> None:
    if any((root / name).exists() for name in (STATE, READY, TESTED, CLEAN)):
        raise RuntimeError("fresh environment unexpectedly contains fixture state")
    write_atomic(
        root / STATE,
        {"run_id": run_id, "schema_version": 1, "seed": ["FIELD-NOTES", "ALPINE-MUG"]},
    )


def ready(root: Path, run_id: str) -> None:
    state = read_owned(root / STATE, run_id)
    if state.get("schema_version") != 1 or state.get("seed") != ["FIELD-NOTES", "ALPINE-MUG"]:
        raise RuntimeError("fixture schema or seed is not ready")
    write_atomic(root / READY, {"run_id": run_id, "ready": True})


def cleanup(root: Path, run_id: str) -> None:
    marker = root / CLEAN
    if marker.exists():
        read_owned(marker, run_id)
    else:
        state = root / STATE
        if state.exists():
            read_owned(state, run_id)
        write_atomic(marker, {"run_id": run_id, "cleanup_complete": True})
    for name in (STATE, READY, TESTED):
        path = root / name
        if path.is_symlink():
            raise RuntimeError(f"refusing to remove linked fixture state: {name}")
        path.unlink(missing_ok=True)


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] not in {"prepare", "ready", "cleanup"}:
        print("Usage: ops.py {prepare,ready,cleanup} ENVIRONMENT", file=sys.stderr)
        return 2
    try:
        root, run_id = environment(argv[1])
        operations = {"prepare": prepare, "ready": ready, "cleanup": cleanup}
        operations[argv[0]](root, run_id)
        return 0
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"{argv[0]} failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
