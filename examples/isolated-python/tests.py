"""Dependency-free JUnit-producing checks that execute inside the isolated container."""

import os
import socket
import sys
import tempfile
import time
import traceback
from collections.abc import Callable
from pathlib import Path
from xml.etree.ElementTree import Element, ElementTree, SubElement

from app import allocate
from ops import read_owned, write_atomic


def test_allocation_is_atomic() -> None:
    stock = {"FIELD-NOTES": 5, "ALPINE-MUG": 2}
    confirmed = allocate(stock, {"FIELD-NOTES": 2, "ALPINE-MUG": 1})
    assert confirmed.status == "confirmed"
    assert confirmed.total_units == 3
    assert confirmed.remaining == {"FIELD-NOTES": 3, "ALPINE-MUG": 1}
    assert stock == {"FIELD-NOTES": 5, "ALPINE-MUG": 2}

    rejected = allocate(stock, {"FIELD-NOTES": 1, "ALPINE-MUG": 99})
    assert rejected.status == "rejected"
    assert rejected.reason == "insufficient:ALPINE-MUG"
    assert rejected.remaining == stock


def test_source_snapshot_is_explicit_and_read_only() -> None:
    source = Path(__file__).resolve().parent
    names = {path.name for path in source.iterdir()}
    assert names == {"app.py", "ops.py", "tests.py"}
    assert not (source / "excluded-host-file.txt").exists()
    assert not (source / ".git").exists()
    try:
        with (source / "app.py").open("a", encoding="utf-8") as stream:
            stream.write("# source must be immutable\n")
    except OSError:
        pass
    else:
        raise AssertionError("source snapshot was writable")


def test_process_boundary_is_locked_down() -> None:
    if not hasattr(os, "getuid") or os.getuid() == 0:
        raise AssertionError("isolated process must run as a non-root Linux user")
    status = {}
    for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            status[key] = value.strip()
    assert int(status["CapEff"], 16) == 0
    assert status["NoNewPrivs"] == "1"
    assert "KATYDID_HOST_SECRET" not in os.environ
    assert "DOCKER_CONFIG" not in os.environ
    assert "GITHUB_TOKEN" not in os.environ
    assert not Path("/var/run/docker.sock").exists()
    assert not Path.home().joinpath(".docker").exists()
    try:
        socket.create_connection(("1.1.1.1", 53), timeout=0.5).close()
    except OSError:
        pass
    else:
        raise AssertionError("container unexpectedly reached an external network")
    try:
        Path("/katydid-root-write").write_text("blocked", encoding="utf-8")
    except OSError:
        pass
    else:
        raise AssertionError("container root filesystem was writable")
    with tempfile.NamedTemporaryFile(dir="/tmp") as stream:
        stream.write(b"bounded tmpfs is writable")


def test_run_owned_environment_is_writable(environment: Path, run_id: str) -> None:
    state = read_owned(environment / "state.json", run_id)
    ready = read_owned(environment / "ready.json", run_id)
    assert state["schema_version"] == 1
    assert ready["ready"] is True
    write_atomic(environment / "tested.json", {"run_id": run_id, "tests_complete": True})


def run_case(suite: Element, name: str, case: Callable[[], None]) -> bool:
    started = time.monotonic()
    node = SubElement(suite, "testcase", name=name, classname="isolated-python")
    try:
        case()
    except BaseException:
        failure = SubElement(node, "failure", message="isolated fixture assertion failed")
        failure.text = traceback.format_exc()
        node.set("time", f"{time.monotonic() - started:.6f}")
        return False
    node.set("time", f"{time.monotonic() - started:.6f}")
    return True


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("Usage: tests.py ENVIRONMENT JUNIT_REPORT", file=sys.stderr)
        return 2
    environment = Path(argv[0]).resolve(strict=True)
    report = Path(argv[1])
    run_id = os.environ.get("KATYDID_RUN_ID", "")
    if environment != Path(os.environ.get("KATYDID_ENVIRONMENT_DIR", "")).resolve(strict=True):
        print("Environment argument does not match KATYDID_ENVIRONMENT_DIR", file=sys.stderr)
        return 2
    root = Element("testsuites")
    suite = SubElement(root, "testsuite", name="isolated-python")
    cases: list[tuple[str, Callable[[], None]]] = [
        ("allocation is atomic", test_allocation_is_atomic),
        (
            "source snapshot is explicit and read only",
            test_source_snapshot_is_explicit_and_read_only,
        ),
        ("process boundary is locked down", test_process_boundary_is_locked_down),
        (
            "run owned environment is writable",
            lambda: test_run_owned_environment_is_writable(environment, run_id),
        ),
    ]
    passed = [run_case(suite, name, case) for name, case in cases]
    suite.set("tests", str(len(cases)))
    suite.set("failures", str(passed.count(False)))
    suite.set("errors", "0")
    suite.set("skipped", "0")
    root.set("tests", str(len(cases)))
    root.set("failures", str(passed.count(False)))
    root.set("errors", "0")
    root.set("skipped", "0")
    report.parent.mkdir(parents=True, exist_ok=True)
    ElementTree(root).write(report, encoding="utf-8", xml_declaration=True)
    print(f"isolated fixture: {passed.count(True)}/{len(cases)} cases passed")
    return 0 if all(passed) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
