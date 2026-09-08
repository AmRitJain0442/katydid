"""Opt-in real Docker containment and abandoned-resource acceptance tests."""

import json
import os
import re
import runpy
import stat
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from katydid.evidence import Status
from katydid.profile import make_plan
from katydid.runner import Run, run_plan

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "isolated-python"
EXPECTED_IMAGE = "python@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36"
IMAGE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._:/-]*@sha256:[a-f0-9]{64}$")


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["docker", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        pytest.fail(f"KATYDID_DOCKER_IMAGE is set but Docker cannot run: {exc}")
    if check and result.returncode != 0:
        pytest.fail(result.stderr.strip() or result.stdout.strip() or "Docker command failed")
    return result


@pytest.fixture(scope="module")
def docker_image() -> str:
    image = os.environ.get("KATYDID_DOCKER_IMAGE")
    if image is None:
        pytest.skip("set KATYDID_DOCKER_IMAGE to opt into real Docker integration tests")
    if not IMAGE_PATTERN.fullmatch(image):
        pytest.fail("KATYDID_DOCKER_IMAGE must be a digest-pinned image reference")
    if image != EXPECTED_IMAGE:
        pytest.fail(f"This fixture requires its pinned Python image: {EXPECTED_IMAGE}")
    daemon = docker("version", "--format", "{{.Server.Os}}", check=False)
    if daemon.returncode != 0 or daemon.stdout.strip() != "linux":
        pytest.fail("KATYDID_DOCKER_IMAGE is set but a Linux Docker daemon is unavailable")
    installed = docker("image", "inspect", image, check=False)
    if installed.returncode != 0:
        pytest.fail("KATYDID_DOCKER_IMAGE is set but the pinned image is not installed locally")
    return image


def wait_for(value: Callable[[], Any], timeout: float = 20) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = value()
        if found:
            return found
        time.sleep(0.05)
    pytest.fail("Timed out waiting for real Docker state")


def container_ids(run: Run) -> list[str]:
    assert run.isolation is not None
    return [str(resource["id"]) for resource in run.isolation["resources"]]


def assert_containers_removed(ids: list[str]) -> None:
    for container_id in ids:
        assert docker("inspect", container_id, check=False).returncode != 0


def write_profile(
    root: Path,
    image: str,
    namespace: str,
    script: str,
    *,
    timeout_seconds: int,
) -> Path:
    profile = {
        "schema_version": 1,
        "repository": "docker-integration",
        "owner": "runtime-team",
        "isolation": {
            "adapter": "docker",
            "image": image,
            "files": [script],
            "namespace": namespace,
            "cpus": 1.0,
            "memory_mb": 512,
            "pids_limit": 128,
            "tmpfs_mb": 64,
        },
        "checks": [
            {
                "id": "probe",
                "kind": "command",
                "argv": ["python", script],
                "timeout_seconds": timeout_seconds,
                "stages": ["pull-request"],
                "required": True,
            }
        ],
    }
    path = root / "quality.json"
    path.write_text(json.dumps(profile), encoding="utf-8")
    return path


def test_real_fixture_has_fresh_junit_lifecycle_and_no_host_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, docker_image: str
) -> None:
    assert docker_image == EXPECTED_IMAGE
    monkeypatch.setenv("KATYDID_HOST_SECRET", "must-not-enter-container")
    monkeypatch.setenv("GITHUB_TOKEN", "must-not-enter-container")
    run = run_plan(make_plan(EXAMPLE / "quality.yaml", "pull-request"), tmp_path / "runs")

    assert run.gate.passed
    assert run.results[0].status == Status.PASSED
    assert run.results[0].evidence is not None
    assert run.results[0].evidence.tests == 4
    assert run.environment is not None
    assert run.environment.ready and run.environment.cleanup_complete
    assert run.isolation is not None
    assert run.isolation["ready"] and run.isolation["cleanup_complete"]
    assert run.isolation["errors"] == []
    assert len(run.isolation["resources"]) == 4
    assert all(resource["removed"] is True for resource in run.isolation["resources"])
    assert_containers_removed(container_ids(run))

    environment = run.directory / "environment" / "data"
    assert {path.name for path in environment.iterdir()} == {"cleanup.json"}
    cleanup_path = environment / "cleanup.json"
    if os.name != "nt":
        assert cleanup_path.stat().st_mode & stat.S_IROTH
    cleanup = json.loads(cleanup_path.read_text(encoding="utf-8"))
    assert cleanup == {"cleanup_complete": True, "run_id": run.id}
    report = run.directory / "000-isolated-tests" / "output" / "junit.xml"
    assert report.is_file()
    assert 'tests="4"' in report.read_text(encoding="utf-8")


def test_controller_repairs_from_a_fresh_isolated_candidate(
    tmp_path: Path, docker_image: str
) -> None:
    helpers = runpy.run_path(str(ROOT / "tests" / "test_controller.py"))
    make_repository = helpers["make_repository"]
    repository_policy = helpers["repository_policy"]
    make_controller = helpers["make_controller"]
    git = helpers["git"]
    provider = helpers["DeterministicTestDouble"]()

    repository = make_repository(tmp_path, "isolated-controller")
    verifier = (repository / "verify.py").read_text(encoding="utf-8")
    namespace = f"controller-{uuid.uuid4().hex[:10]}"
    profile_path = repository / "quality.yaml"
    profile = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    profile["isolation"] = {
        "adapter": "docker",
        "image": docker_image,
        "files": ["app.py", "verify.py"],
        "namespace": namespace,
        "cpus": 1.0,
        "memory_mb": 512,
        "pids_limit": 128,
        "tmpfs_mb": 64,
    }
    profile_path.write_text(yaml.safe_dump(profile), encoding="utf-8")
    git(repository, "add", "quality.yaml")
    git(repository, "commit", "-m", "run the protected verifier in Docker")

    policy = repository_policy(repository)
    policy["isolation_policy"] = {
        "required": True,
        "images": [docker_image],
        "files": ["app.py", "verify.py"],
        "namespace": namespace,
        "max_cpus": 1.0,
        "max_memory_mb": 512,
        "max_pids": 128,
        "max_tmpfs_mb": 64,
    }
    controller = make_controller(tmp_path, [policy], provider)
    task = controller.enqueue("isolated-controller", "real-docker-repair")

    completed = controller.work_once()

    assert completed is not None
    assert completed["id"] == task["id"]
    assert completed["state"] == "completed"
    result = completed["result"]
    assert result["outcome"] == "repaired"
    assert result["baseline"]["gate"]["passed"] is False
    assert result["verification"]["gate"]["passed"] is True
    assert result["baseline"]["results"][0]["status"] == "failed"
    assert result["verification"]["results"][0]["status"] == "passed"
    assert provider.roles == ["diagnosis", "repair", "review"]

    baseline_directory = Path(result["baseline"]["directory"])
    verification_directory = Path(result["verification"]["directory"])
    assert baseline_directory != verification_directory
    baseline_source = baseline_directory / "isolation" / "source"
    verification_source = verification_directory / "isolation" / "source"
    assert baseline_source != verification_source
    assert (baseline_source / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert (verification_source / "app.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    assert (baseline_source / "verify.py").read_text(encoding="utf-8") == verifier
    assert (verification_source / "verify.py").read_text(encoding="utf-8") == verifier

    for phase in ("baseline", "verification"):
        isolation = result[phase]["isolation"]
        assert isolation["ready"] is True
        assert isolation["cleanup_complete"] is True
        assert isolation["errors"] == []
        assert len(isolation["resources"]) == 1
        assert isolation["resources"][0]["removed"] is True
        assert_containers_removed([isolation["resources"][0]["id"]])

    assert (repository / "app.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    assert (repository / "verify.py").read_text(encoding="utf-8") == verifier
    assert result["published_sha"] == result["candidate_sha"]
    assert result["merged_sha"] == result["candidate_sha"]
    assert "release" not in result


def test_live_container_limits_and_cancellation_remove_container(
    tmp_path: Path, docker_image: str
) -> None:
    namespace = f"limits-{uuid.uuid4().hex[:10]}"
    script = tmp_path / "hold.py"
    script.write_text(
        "import time\nprint('ready', flush=True)\ntime.sleep(300)\n", encoding="utf-8"
    )
    profile = write_profile(tmp_path, docker_image, namespace, script.name, timeout_seconds=300)
    cancel = threading.Event()
    run_directories: list[Path] = []
    completed: list[Run] = []
    failures: list[BaseException] = []

    def execute() -> None:
        try:
            completed.append(
                run_plan(
                    make_plan(profile, "pull-request"),
                    tmp_path / "runs",
                    cancel,
                    run_directories.append,
                )
            )
        except BaseException as exc:
            failures.append(exc)

    worker = threading.Thread(target=execute)
    worker.start()
    try:
        run_directory = wait_for(lambda: run_directories[0] if run_directories else None)
        metadata_path = wait_for(
            lambda: next(iter(run_directory.glob("000-probe/container.json")), None)
        )
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        container_id = metadata["id"]
        inspected = json.loads(docker("inspect", container_id).stdout)[0]
        host = inspected["HostConfig"]
        assert inspected["Config"]["User"] == "65532:65532"
        assert host["NetworkMode"] == "none"
        assert host["ReadonlyRootfs"] is True
        assert host["CapDrop"] == ["ALL"]
        assert "no-new-privileges" in host["SecurityOpt"]
        assert host["NanoCpus"] == 1_000_000_000
        assert host["Memory"] == 512 * 1024 * 1024
        assert host["PidsLimit"] == 128
        assert "/tmp" in host["Tmpfs"]
        assert any(size in host["Tmpfs"]["/tmp"] for size in ("size=64m", "size=67108864"))
        mounts = {mount["Destination"]: mount for mount in inspected["Mounts"]}
        assert mounts["/workspace"]["RW"] is False
        assert mounts["/output"]["RW"] is True
        assert set(mounts) == {"/workspace", "/output"}
        cancel.set()
        worker.join(timeout=30)
        assert not worker.is_alive()
        assert failures == []
        assert len(completed) == 1
        run = completed[0]
        assert run.cancelled
        assert run.results[0].status == Status.CANCELLED
        assert_containers_removed(container_ids(run))
    finally:
        cancel.set()
        worker.join(timeout=30)


def test_timeout_removes_container_with_detached_child(tmp_path: Path, docker_image: str) -> None:
    namespace = f"timeout-{uuid.uuid4().hex[:10]}"
    script = tmp_path / "hang.py"
    script.write_text(
        """import subprocess
import sys
import time

subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"], start_new_session=True)
print("detached-child-started", flush=True)
time.sleep(300)
""",
        encoding="utf-8",
    )
    profile = write_profile(tmp_path, docker_image, namespace, script.name, timeout_seconds=5)
    run = run_plan(make_plan(profile, "pull-request"), tmp_path / "runs")

    assert not run.gate.passed
    assert run.results[0].status == Status.TIMED_OUT
    assert "detached-child-started" in (run.directory / "000-probe" / "stdout.log").read_text(
        encoding="utf-8"
    )
    assert run.isolation is not None and run.isolation["cleanup_complete"]
    assert_containers_removed(container_ids(run))


def create_decoy(
    image: str,
    name: str,
    labels: dict[str, str] | None = None,
) -> str:
    args = ["create", "--name", name]
    for key, value in (labels or {}).items():
        args.extend(("--label", f"{key}={value}"))
    args.extend((image, "python", "-c", "pass"))
    return docker(*args).stdout.strip()


def test_hard_killed_cli_is_recovered_by_scoped_sweeper(tmp_path: Path, docker_image: str) -> None:
    from katydid.docker import EXPIRY_LABEL, MANAGED_LABEL, NAMESPACE_LABEL, RUN_LABEL

    namespace = f"crash-{uuid.uuid4().hex[:10]}"
    other_namespace = f"other-{uuid.uuid4().hex[:10]}"
    script = tmp_path / "crash.py"
    script.write_text(
        "import time\nprint('running', flush=True)\ntime.sleep(300)\n", encoding="utf-8"
    )
    profile = write_profile(tmp_path, docker_image, namespace, script.name, timeout_seconds=300)
    output = tmp_path / "crash-runs"
    launcher = (
        "import sys,time as clock; import katydid.docker as adapter; "
        "adapter._now=lambda: clock.time()-7200; "
        "from katydid.cli import main; "
        "raise SystemExit(main(['run',sys.argv[1],'--output',sys.argv[2]]))"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", launcher, str(profile), str(output)],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    created_ids: list[str] = []
    crash_id: str | None = None
    try:
        metadata_path = wait_for(
            lambda: next(iter(output.glob("*/000-probe/container.json")), None)
        )
        crash_id = json.loads(metadata_path.read_text(encoding="utf-8"))["id"]
        wait_for(lambda: json.loads(docker("inspect", crash_id).stdout)[0]["State"]["Running"])
        process.kill()
        process.communicate(timeout=10)
        assert docker("inspect", crash_id, check=False).returncode == 0

        def labelled(name_namespace: str, run_id: str, expiry: str) -> dict[str, str]:
            return {
                MANAGED_LABEL: "true",
                NAMESPACE_LABEL: name_namespace,
                RUN_LABEL: run_id,
                EXPIRY_LABEL: expiry,
            }

        suffixes = [uuid.uuid4().hex[:12] for _ in range(4)]
        unlabelled = create_decoy(docker_image, f"unlabelled-{suffixes[0]}")
        created_ids.append(unlabelled)
        malformed_run = f"malformed-{uuid.uuid4().hex[:8]}"
        malformed_name = f"katydid-{namespace}-{malformed_run[:12]}-{suffixes[1]}"
        malformed = create_decoy(
            docker_image,
            malformed_name,
            labelled(namespace, malformed_run, "not-a-time"),
        )
        created_ids.append(malformed)
        other_run = f"other-{uuid.uuid4().hex[:8]}"
        other_name = f"katydid-{other_namespace}-{other_run[:12]}-{suffixes[2]}"
        other = create_decoy(
            docker_image,
            other_name,
            labelled(other_namespace, other_run, "0"),
        )
        created_ids.append(other)
        live_run = f"live-{uuid.uuid4().hex[:8]}"
        live_name = f"katydid-{namespace}-{live_run[:12]}-{suffixes[3]}"
        unexpired = create_decoy(
            docker_image,
            live_name,
            labelled(namespace, live_run, str(int(time.time()) + 3600)),
        )
        created_ids.append(unexpired)

        watcher = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "katydid",
                "sweep",
                "--namespace",
                namespace,
                "--watch",
                "--interval",
                "1",
            ],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        try:
            wait_for(lambda: docker("inspect", crash_id, check=False).returncode != 0)
            assert watcher.stdout is not None
            watched = json.loads(watcher.stdout.readline())
            assert watched["errors"] == []
            assert crash_id in watched["removed"]
            assert malformed in watched["skipped"]
            assert unexpired in watched["skipped"]
        finally:
            watcher.terminate()
            watcher.communicate(timeout=10)

        swept = subprocess.run(
            [sys.executable, "-m", "katydid", "sweep", "--namespace", namespace],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )
        assert swept.returncode == 0, swept.stderr
        repeated = json.loads(swept.stdout)
        assert repeated["errors"] == []
        assert repeated["removed"] == []
        assert malformed in repeated["skipped"]
        assert unexpired in repeated["skipped"]
        for decoy in created_ids:
            assert docker("inspect", decoy, check=False).returncode == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)
        if crash_id is not None:
            docker("rm", "--force", crash_id, check=False)
        for container_id in created_ids:
            docker("rm", "--force", container_id, check=False)
