import io
import json
import os
import stat
import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest

from katydid import docker
from katydid.evidence import Status
from katydid.profile import Plan, PlannedCheck, PlannedIsolation

IMAGE_DIGEST = "b" * 64
IMAGE = f"python:3.12@sha256:{IMAGE_DIGEST}"
IMAGE_ID = "sha256:" + "a" * 64
CONTAINER_ID = "c" * 64


def completed(
    args: list[str], returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["docker", *args], returncode, stdout, stderr)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def plan(root: Path, *, files: tuple[str, ...] = ("app.py",), namespace: str = "katydid") -> Plan:
    check = PlannedCheck("tests", "test", ("{python}", "app.py", "{report}"), str(root), 5, True)
    return Plan(
        1,
        "sample",
        "owner",
        "pull-request",
        str(root),
        str(root / "quality.yaml"),
        "0" * 64,
        (check,),
        (),
        isolation=PlannedIsolation(IMAGE, files, namespace),
    )


class FakeProcess:
    pid = 1234

    def __init__(self, action: Any = None, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.returncode: int | None = 0
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO(stderr)
        if action is not None:
            action()

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        self.returncode = 0
        return 0

    def kill(self) -> None:
        self.returncode = -9


class HangingProcess(FakeProcess):
    def __init__(self, stdout: bytes = b"", stderr: bytes = b"") -> None:
        super().__init__(stdout=stdout, stderr=stderr)
        self.returncode = None
        self.killed = False

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


class FakeDocker:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.raw_calls: list[list[str]] = []
        self.containers: dict[str, dict[str, Any]] = {}
        self.removed: list[str] = []
        self.context_host = "npipe:////./pipe/dockerDesktopLinuxEngine"
        self.server: dict[str, Any] = {"Os": "linux", "Version": "27.1.0"}
        self.volumes: Any = None
        self.repo_digests: list[str] = [f"python@sha256:{IMAGE_DIGEST}"]
        self.create_result = 0
        self.inspect_state: dict[str, Any] = {
            "Running": False,
            "ExitCode": 0,
            "OOMKilled": False,
            "Error": "",
        }

    def __call__(
        self, args: list[str], timeout: float = docker.DOCKER_TIMEOUT_SECONDS
    ) -> subprocess.CompletedProcess[str]:
        del timeout
        self.raw_calls.append(args)
        if args[:1] == ["--host"]:
            args = args[2:]
        self.calls.append(args)
        if args[:2] == ["context", "inspect"]:
            return completed(args, stdout=json.dumps(self.context_host))
        if args[:2] == ["version", "--format"]:
            return completed(args, stdout=json.dumps(self.server))
        if args[:2] == ["image", "inspect"]:
            value = {
                "Id": IMAGE_ID,
                "RepoDigests": self.repo_digests,
                "Os": "linux",
                "Config": {"Volumes": self.volumes},
            }
            return completed(args, stdout=json.dumps([value]))
        if args[0] == "create":
            name = args[args.index("--name") + 1]
            labels = {}
            for index, value in enumerate(args):
                if value == "--label":
                    key, item = args[index + 1].split("=", 1)
                    labels[key] = item
            self.containers[CONTAINER_ID] = {
                "Id": CONTAINER_ID,
                "Name": f"/{name}",
                "Config": {"Labels": labels},
                "State": dict(self.inspect_state),
            }
            stdout = CONTAINER_ID if self.create_result == 0 else ""
            return completed(args, self.create_result, stdout, "lost create response")
        if args[0] == "inspect":
            identifier = args[1]
            value = self.containers.get(identifier)
            if value is None:
                value = next(
                    (
                        item
                        for item in self.containers.values()
                        if item["Name"].lstrip("/") == identifier
                    ),
                    None,
                )
            if value is None:
                return completed(args, 1, stderr="Error: No such object")
            return completed(args, stdout=json.dumps([value]))
        if args[:2] == ["rm", "--force"]:
            identifier = args[2]
            value = self.containers.pop(identifier, None)
            if value is None:
                return completed(args, 1, stderr="Error: No such object")
            self.removed.append(identifier)
            return completed(args, stdout=identifier)
        if args[0] == "ps":
            return completed(args, stdout="\n".join(self.containers))
        raise AssertionError(f"Unexpected Docker call: {args}")


def prepared_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeDocker | None = None
) -> tuple[docker.DockerSession, FakeDocker, Path]:
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("print('ok')\n", encoding="utf-8")
    (source / "quality.yaml").write_text("profile", encoding="utf-8")
    backend = fake or FakeDocker()
    monkeypatch.setattr(docker, "_docker_run", backend)
    session = docker.DockerSession(plan(source), tmp_path / "run", "run_123", write_json)
    session.prepare()
    return session, backend, source


def passing_report(folder: Path) -> None:
    (folder / "output" / "junit.xml").write_text(
        '<testsuite tests="1"><testcase name="real"/></testsuite>', encoding="utf-8"
    )


def test_prepare_preflights_local_linux_and_snapshots_only_explicit_files(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    executable = source / "app.py"
    executable.write_text("print('safe')\n", encoding="utf-8")
    executable.chmod(0o755)
    (source / "unlisted.txt").write_text("secret", encoding="utf-8")
    (source / "quality.yaml").write_text("profile", encoding="utf-8")
    fake = FakeDocker()
    monkeypatch.setattr(docker, "_docker_run", fake)
    session = docker.DockerSession(plan(source), tmp_path / "run", "run-1", write_json)

    session.prepare()

    copied = session.source_directory / "app.py"
    assert copied.read_text(encoding="utf-8") == "print('safe')\n"
    assert not (session.source_directory / "unlisted.txt").exists()
    if os.name != "nt":
        assert copied.stat().st_mode & stat.S_IXUSR
        assert not copied.stat().st_mode & stat.S_IWUSR
    state = session.snapshot()
    assert state["ready"] is True
    assert state["cleanup_complete"] is True
    assert state["image_id"] == IMAGE_ID
    assert state["daemon"] == {
        "os": "linux",
        "version": "27.1.0",
        "host": fake.context_host,
    }
    assert not any("pull" in call for call in fake.calls)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda fake: setattr(fake, "context_host", "ssh://remote"), "local"),
        (lambda fake: fake.server.update(Os="windows"), "Linux"),
        (lambda fake: setattr(fake, "repo_digests", []), "repository digest"),
        (lambda fake: setattr(fake, "volumes", {"/data": {}}), "automatic volumes"),
    ],
)
def test_prepare_rejects_unsafe_daemon_or_image(tmp_path, monkeypatch, change, message):
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("safe", encoding="utf-8")
    fake = FakeDocker()
    change(fake)
    monkeypatch.setattr(docker, "_docker_run", fake)
    session = docker.DockerSession(plan(source), tmp_path / "run", "run-1", write_json)

    with pytest.raises(docker.DockerError, match=message):
        session.prepare()

    assert session.snapshot()["ready"] is False
    assert session.snapshot()["errors"]
    assert not any(call[0] in ("create", "pull") for call in fake.calls)


def test_prepare_rejects_remote_docker_host_override(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("safe", encoding="utf-8")
    fake = FakeDocker()
    monkeypatch.setattr(docker, "_docker_run", fake)
    monkeypatch.setenv("DOCKER_HOST", "tcp://remote.example:2376")
    session = docker.DockerSession(plan(source), tmp_path / "run", "run-1", write_json)

    with pytest.raises(docker.DockerError, match="local"):
        session.prepare()

    assert not any(call[0] == "create" for call in fake.calls)


def test_prepare_rejects_same_digest_from_a_different_repository(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("safe", encoding="utf-8")
    fake = FakeDocker()
    fake.repo_digests = [f"untrusted/example@sha256:{IMAGE_DIGEST}"]
    monkeypatch.setattr(docker, "_docker_run", fake)
    session = docker.DockerSession(plan(source), tmp_path / "run", "run-1", write_json)

    with pytest.raises(docker.DockerError, match="repository digest"):
        session.prepare()


def test_source_snapshot_has_a_bounded_actual_read(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_bytes(b"12345")
    fake = FakeDocker()
    monkeypatch.setattr(docker, "_docker_run", fake)
    monkeypatch.setattr(docker, "SOURCE_LIMIT_BYTES", 4)
    session = docker.DockerSession(plan(source), tmp_path / "run", "run-1", write_json)

    with pytest.raises(docker.DockerError, match="64 MiB"):
        session.prepare()


def test_execute_uses_strict_flags_translates_paths_and_removes_by_id(tmp_path, monkeypatch):
    session, fake, source = prepared_session(tmp_path, monkeypatch)
    monkeypatch.setattr(docker.secrets, "token_hex", lambda _: "123456789abc")
    monkeypatch.setattr(docker, "_now", lambda: 1000.9)
    monkeypatch.setattr(
        docker,
        "_docker_start",
        lambda container_id, host: FakeProcess(lambda: passing_report(folder)),
    )
    folder = tmp_path / "run" / "check"
    folder.mkdir()
    check = plan(source).checks[0]

    result = session.execute(check, folder, threading.Event(), tmp_path / "cancel")

    assert result.status == Status.PASSED
    assert result.exit_code == 0
    assert result.evidence is not None and result.evidence.tests == 1
    create = next(call for call in fake.calls if call[0] == "create")
    for option in (
        "--network",
        "--read-only",
        "--cap-drop",
        "--security-opt",
        "--memory-swap",
        "--pids-limit",
        "--no-healthcheck",
        "--entrypoint",
    ):
        assert option in create
    assert create[create.index("--network") + 1] == "none"
    assert create[create.index("--user") + 1] == "65532:65532"
    assert create[create.index("--entrypoint") + 1] == "python3"
    assert create[-1] == "/output/junit.xml"
    assert IMAGE_ID in create
    assert not any(str(source) == value for value in create)
    assert fake.removed == [CONTAINER_ID]
    assert session.snapshot()["cleanup_complete"] is True
    record = json.loads((folder / "container.json").read_text(encoding="utf-8"))
    assert record["id"] == CONTAINER_ID
    assert record["expires_at"] == 1065
    assert record["removed"] is True


def test_preflight_pins_endpoint_for_every_later_operation(tmp_path, monkeypatch):
    session, fake, source = prepared_session(tmp_path, monkeypatch)
    pinned = fake.context_host
    fake.context_host = "ssh://changed-after-preflight"
    folder = tmp_path / "run" / "check"
    folder.mkdir()
    started_with: list[str] = []

    def start(container_id: str, host: str) -> FakeProcess:
        started_with.append(host)
        return FakeProcess(lambda: passing_report(folder))

    monkeypatch.setattr(docker, "_docker_start", start)

    result = session.execute(plan(source).checks[0], folder, threading.Event(), tmp_path / "cancel")

    assert result.status == Status.PASSED
    assert started_with == [pinned]
    later = fake.raw_calls[3:]
    assert later
    assert all(call[:2] == ["--host", pinned] for call in later)


def test_cancel_before_create_launches_nothing(tmp_path, monkeypatch):
    session, fake, source = prepared_session(tmp_path, monkeypatch)
    cancel = threading.Event()
    cancel.set()
    folder = tmp_path / "run" / "check"
    folder.mkdir()

    result = session.execute(plan(source).checks[0], folder, cancel, tmp_path / "cancel")

    assert result.status == Status.CANCELLED
    assert not any(call[0] == "create" for call in fake.calls)
    assert session.snapshot()["resources"] == []


def test_lost_create_response_recovers_exact_name_and_removes_verified_id(tmp_path, monkeypatch):
    fake = FakeDocker()
    fake.create_result = 1
    session, fake, source = prepared_session(tmp_path, monkeypatch, fake)
    monkeypatch.setattr(docker, "_docker_start", lambda *args: pytest.fail("must not start"))
    folder = tmp_path / "run" / "check"
    folder.mkdir()

    result = session.execute(plan(source).checks[0], folder, threading.Event(), tmp_path / "cancel")

    assert result.status == Status.ERROR
    assert fake.removed == [CONTAINER_ID]
    assert session.snapshot()["resources"][0]["id"] == CONTAINER_ID
    assert session.snapshot()["cleanup_complete"] is True


def test_journal_failure_after_create_does_not_skip_remote_cleanup(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("safe", encoding="utf-8")
    fake = FakeDocker()
    monkeypatch.setattr(docker, "_docker_run", fake)
    isolation_writes = 0

    def fail_one_journal(path: Path, value: Any) -> None:
        nonlocal isolation_writes
        if path.name == "isolation.json":
            isolation_writes += 1
            if isolation_writes == 3:
                raise OSError("disk unavailable")
        write_json(path, value)

    session = docker.DockerSession(plan(source), tmp_path / "run", "run-1", fail_one_journal)
    session.prepare()
    monkeypatch.setattr(docker, "_docker_start", lambda *args: pytest.fail("must not start"))
    folder = tmp_path / "run" / "check"
    folder.mkdir()

    result = session.execute(plan(source).checks[0], folder, threading.Event(), tmp_path / "cancel")

    assert result.status == Status.ERROR
    assert fake.removed == [CONTAINER_ID]
    assert session.snapshot()["cleanup_complete"] is True


def test_persistent_journal_failure_still_stops_client_and_capture_threads(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("safe", encoding="utf-8")
    fake = FakeDocker()
    monkeypatch.setattr(docker, "_docker_run", fake)
    isolation_writes = 0

    def fail_persistently(path: Path, value: Any) -> None:
        nonlocal isolation_writes
        if path.name == "isolation.json":
            isolation_writes += 1
            if isolation_writes >= 4:
                raise OSError("disk remains unavailable")
        write_json(path, value)

    session = docker.DockerSession(plan(source), tmp_path / "run", "run-1", fail_persistently)
    session.prepare()
    monkeypatch.setattr(docker, "MAX_LOG_BYTES", 1)
    process = HangingProcess(stdout=b"too much", stderr=b"also too much")
    monkeypatch.setattr(docker, "_docker_start", lambda container_id, host: process)
    folder = tmp_path / "run" / "check"
    folder.mkdir()

    with pytest.raises(docker.DockerError, match="persist Docker cleanup"):
        session.execute(plan(source).checks[0], folder, threading.Event(), tmp_path / "cancel")

    assert fake.removed == [CONTAINER_ID]
    assert process.killed is True
    assert not any(thread.name.startswith("katydid-docker-") for thread in threading.enumerate())


def test_cleanup_attempts_every_resource_before_propagating_persistent_journal_failure(
    tmp_path, monkeypatch
):
    session, _, _ = prepared_session(tmp_path, monkeypatch)
    first = session._resource("katydid-run-123-111111111111", 1)
    second = session._resource("katydid-run-123-222222222222", 1)
    session.resources.extend((first, second))
    attempted: list[str] = []

    def remove(resource):
        attempted.append(resource["name"])
        resource["removed"] = True
        raise OSError("journal unavailable")

    monkeypatch.setattr(session, "_remove", remove)
    monkeypatch.setattr(
        session, "write_json", lambda path, value: (_ for _ in ()).throw(OSError("full"))
    )

    with pytest.raises(docker.DockerError, match="persist Docker cleanup"):
        session.cleanup()

    assert attempted == [first["name"], second["name"]]


def test_label_change_blocks_removal_and_marks_session_unsafe(tmp_path, monkeypatch):
    session, fake, source = prepared_session(tmp_path, monkeypatch)
    original = fake.__call__

    def tampering(args, timeout=docker.DOCKER_TIMEOUT_SECONDS):
        result = original(args, timeout)
        if fake.calls[-1][0] == "create":
            fake.containers[CONTAINER_ID]["Config"]["Labels"][docker.RUN_LABEL] = "other"
        return result

    monkeypatch.setattr(docker, "_docker_run", tampering)
    monkeypatch.setattr(docker, "_docker_start", lambda *args: pytest.fail("must not start"))
    folder = tmp_path / "run" / "check"
    folder.mkdir()

    result = session.execute(plan(source).checks[0], folder, threading.Event(), tmp_path / "cancel")

    assert result.status == Status.ERROR
    assert fake.removed == []
    assert session.snapshot()["cleanup_complete"] is False
    assert any("ownership labels" in error for error in session.snapshot()["errors"])


def test_oom_is_an_infrastructure_error_even_with_a_process_exit(tmp_path, monkeypatch):
    fake = FakeDocker()
    fake.inspect_state["OOMKilled"] = True
    session, fake, source = prepared_session(tmp_path, monkeypatch, fake)
    monkeypatch.setattr(docker, "_docker_start", lambda container_id, host: FakeProcess())
    folder = tmp_path / "run" / "check"
    folder.mkdir()

    result = session.execute(plan(source).checks[0], folder, threading.Event(), tmp_path / "cancel")

    assert result.status == Status.ERROR
    assert fake.removed == [CONTAINER_ID]
    assert any("exceeding memory" in error for error in session.snapshot()["errors"])


def test_fast_output_is_hard_bounded_across_both_streams_and_capture_terminates(
    tmp_path, monkeypatch
):
    session, fake, source = prepared_session(tmp_path, monkeypatch)
    monkeypatch.setattr(docker, "MAX_LOG_BYTES", 10)
    monkeypatch.setattr(
        docker,
        "_docker_start",
        lambda container_id, host: FakeProcess(stdout=b"a" * 100, stderr=b"b" * 100),
    )
    folder = tmp_path / "run" / "check"
    folder.mkdir()

    result = session.execute(plan(source).checks[0], folder, threading.Event(), tmp_path / "cancel")

    assert result.status == Status.ERROR
    assert (folder / "stdout.log").stat().st_size + (folder / "stderr.log").stat().st_size == 10
    assert fake.removed == [CONTAINER_ID]
    assert not any(thread.name.startswith("katydid-docker-") for thread in threading.enumerate())
    create = next(call for call in fake.calls if call[0] == "create")
    assert create[create.index("--log-driver") + 1] == "none"
    assert any("logs exceeded" in error for error in session.snapshot()["errors"])


def test_untrusted_junit_symlink_is_not_followed(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    outside = tmp_path / "outside.xml"
    outside.write_text('<testsuite><testcase name="forged"/></testsuite>', encoding="utf-8")
    try:
        (output / "junit.xml").symlink_to(outside)
    except OSError:
        pytest.skip("This Windows account cannot create symlinks")

    evidence = docker._safe_report(output / "junit.xml", tmp_path / "trusted.xml")

    assert evidence.status == Status.INVALID_EVIDENCE
    assert not (tmp_path / "trusted.xml").exists()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are unavailable on this platform")
def test_untrusted_junit_fifo_is_rejected_without_opening_it(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    os.mkfifo(output / "junit.xml")

    evidence = docker._safe_report(output / "junit.xml", tmp_path / "trusted.xml")

    assert evidence.status == Status.INVALID_EVIDENCE
    assert "regular file" in evidence.detail
    assert not (tmp_path / "trusted.xml").exists()


def test_report_copy_is_bounded_before_xml_parsing(tmp_path, monkeypatch):
    output = tmp_path / "output"
    output.mkdir()
    (output / "junit.xml").write_bytes(b"x" * 11)
    monkeypatch.setattr(docker, "MAX_REPORT_BYTES", 10)

    evidence = docker._safe_report(output / "junit.xml", tmp_path / "trusted.xml")

    assert evidence.status == Status.INVALID_EVIDENCE
    assert "10 MiB" in evidence.detail
    assert not (tmp_path / "trusted.xml").exists()


def test_safely_copied_malformed_xml_does_not_become_an_isolation_error(tmp_path, monkeypatch):
    session, _, source = prepared_session(tmp_path, monkeypatch)
    folder = tmp_path / "run" / "check"
    folder.mkdir()

    def malformed() -> None:
        (folder / "output" / "junit.xml").write_text("<not-xml", encoding="utf-8")

    monkeypatch.setattr(docker, "_docker_start", lambda container_id, host: FakeProcess(malformed))

    result = session.execute(plan(source).checks[0], folder, threading.Event(), tmp_path / "cancel")

    assert result.status == Status.INVALID_EVIDENCE
    assert session.snapshot()["errors"] == []


def sweep_container(run_id: str, namespace: str, expiry: str, suffix: str) -> dict[str, Any]:
    return {
        "Id": CONTAINER_ID,
        "Name": f"/katydid-{namespace}-{docker._run_slug(run_id)}-{suffix}",
        "Config": {
            "Labels": {
                docker.MANAGED_LABEL: docker.MANAGED_VALUE,
                docker.NAMESPACE_LABEL: namespace,
                docker.RUN_LABEL: run_id,
                docker.EXPIRY_LABEL: expiry,
            }
        },
        "State": {"Running": True},
    }


def test_sweep_removes_only_expired_exactly_owned_container(tmp_path, monkeypatch):
    del tmp_path
    fake = FakeDocker()
    fake.containers[CONTAINER_ID] = sweep_container("run_123", "katydid", "99", "123456789abc")
    monkeypatch.setattr(docker, "_docker_run", fake)
    monkeypatch.setattr(docker, "_now", lambda: 100.0)

    result = docker.sweep("katydid")

    assert result == {"removed": [CONTAINER_ID], "skipped": [], "errors": []}
    assert fake.removed == [CONTAINER_ID]
    assert not any(call[0] in ("volume", "network", "system") for call in fake.calls)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value["Config"]["Labels"].update({docker.EXPIRY_LABEL: "later"}),
        lambda value: value["Config"]["Labels"].update({docker.NAMESPACE_LABEL: "other"}),
        lambda value: value.update(Name="/unrelated"),
        lambda value: value["Config"]["Labels"].update({docker.EXPIRY_LABEL: "101"}),
    ],
)
def test_sweep_skips_malformed_other_namespace_name_and_unexpired(tmp_path, monkeypatch, mutation):
    del tmp_path
    fake = FakeDocker()
    value = sweep_container("run_123", "katydid", "99", "123456789abc")
    mutation(value)
    fake.containers[CONTAINER_ID] = value
    monkeypatch.setattr(docker, "_docker_run", fake)
    monkeypatch.setattr(docker, "_now", lambda: 100.0)

    result = docker.sweep("katydid")

    assert result == {"removed": [], "skipped": [CONTAINER_ID], "errors": []}
    assert fake.removed == []


def test_cleanup_is_idempotent_when_a_sweeper_already_removed_the_container(tmp_path, monkeypatch):
    session, fake, _ = prepared_session(tmp_path, monkeypatch)
    resource = session._resource("katydid-katydid-run-123-123456789abc", 1)
    resource["id"] = CONTAINER_ID
    session.resources.append(resource)
    session._changed()

    session.cleanup()
    session.cleanup()

    assert session.snapshot()["cleanup_complete"] is True
    assert session.snapshot()["errors"] == []


def test_cleanup_accepts_sweeper_race_between_inspect_and_remove(tmp_path, monkeypatch):
    session, fake, _ = prepared_session(tmp_path, monkeypatch)
    resource = session._resource("katydid-katydid-run-123-123456789abc", 1)
    resource["id"] = CONTAINER_ID
    fake.containers[CONTAINER_ID] = sweep_container("run_123", "katydid", "1", "123456789abc")
    session.resources.append(resource)
    original = fake.__call__

    def raced(args, timeout=docker.DOCKER_TIMEOUT_SECONDS):
        normalized = args[2:] if args[:1] == ["--host"] else args
        if normalized[:2] == ["rm", "--force"]:
            fake.containers.pop(CONTAINER_ID)
            return completed(normalized, 1, stderr="Error: No such container")
        return original(args, timeout)

    monkeypatch.setattr(docker, "_docker_run", raced)

    session.cleanup()

    assert session.snapshot()["cleanup_complete"] is True
    assert session.snapshot()["errors"] == []


def test_sweep_reports_a_hung_daemon_without_raising(monkeypatch):
    def timeout(args, timeout=docker.DOCKER_TIMEOUT_SECONDS):
        raise subprocess.TimeoutExpired(["docker", *args], timeout)

    monkeypatch.setattr(docker, "_docker_run", timeout)

    result = docker.sweep("katydid")

    assert result["removed"] == []
    assert result["skipped"] == []
    assert len(result["errors"]) == 1
    assert "Cannot list" in result["errors"][0]


def test_sweep_refuses_remote_docker_host_before_listing(monkeypatch):
    fake = FakeDocker()
    monkeypatch.setattr(docker, "_docker_run", fake)
    monkeypatch.setenv("DOCKER_HOST", "tcp://remote.example:2376")

    result = docker.sweep("katydid")

    assert result["removed"] == []
    assert len(result["errors"]) == 1
    assert "local" in result["errors"][0]
    assert fake.calls == []
