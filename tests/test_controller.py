import json
import subprocess
import threading
from pathlib import Path

import pytest
import yaml

from katydid.ai import Diagnosis, FileEdit, Repair, Review
from katydid.controller import Controller
from katydid.workspace import source_head


def git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    ).stdout.strip()


VERIFY = """import importlib
import os
from pathlib import Path

application = importlib.import_module("app")
try:
    assert application.VALUE == 2, f"expected VALUE=2, got {application.VALUE!r}"
except AssertionError as exc:
    body = f'<testcase name="value_contract"><failure>{exc}</failure></testcase>'
    code = 1
else:
    body = '<testcase name="value_contract"/>'
    code = 0
report = Path(os.environ["KATYDID_REPORT_PATH"])
report.write_text(f'<testsuite tests="1" failures="{code}">{body}</testsuite>', encoding="utf-8")
raise SystemExit(code)
"""

VERIFY_WITHOUT_EVIDENCE = """import app
raise SystemExit(0 if app.VALUE == 2 else 1)
"""

DEPLOY = """import sys
from pathlib import Path

target = Path(sys.argv[1])
target.mkdir(parents=True, exist_ok=True)
(target / "deployed.txt").write_text("new:" + sys.argv[2], encoding="utf-8")
"""

HEALTH = """import sys
from pathlib import Path

value = (Path(sys.argv[1]) / "deployed.txt").read_text(encoding="utf-8")
raise SystemExit(0 if value == "old" else 1)
"""

ROLLBACK = """import sys
from pathlib import Path

(Path(sys.argv[1]) / "deployed.txt").write_text("old", encoding="utf-8")
"""


def make_repository(
    root: Path,
    name: str,
    *,
    missing_evidence: bool = False,
    release_scripts: bool = False,
) -> Path:
    repository = root / name
    repository.mkdir()
    git(repository, "init", "--initial-branch=main")
    git(repository, "config", "user.name", "Katydid Test")
    git(repository, "config", "user.email", "katydid@example.invalid")
    (repository / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repository / "verify.py").write_text(
        VERIFY_WITHOUT_EVIDENCE if missing_evidence else VERIFY, encoding="utf-8"
    )
    profile = {
        "schema_version": 1,
        "repository": name,
        "owner": "test platform",
        "checks": [
            {
                "id": "unit",
                "kind": "test",
                "argv": ["{python}", "verify.py"],
                "timeout_seconds": 10,
                "stages": ["pull-request", "release"] if release_scripts else ["pull-request"],
            }
        ],
    }
    (repository / "quality.yaml").write_text(yaml.safe_dump(profile), encoding="utf-8")
    if release_scripts:
        (repository / "deploy.py").write_text(DEPLOY, encoding="utf-8")
        (repository / "health.py").write_text(HEALTH, encoding="utf-8")
        (repository / "rollback.py").write_text(ROLLBACK, encoding="utf-8")
    git(repository, "add", ".")
    git(repository, "commit", "-m", "seed failing value contract")
    return repository


def repository_policy(
    repository: Path,
    *,
    repository_id: str | None = None,
    requirements: str = "VALUE must equal 2.",
    repair_attempts: int = 1,
    release: bool = False,
) -> dict:
    policy = {
        "id": repository_id or repository.name,
        "source": repository.name,
        "context_paths": ["app.py", "verify.py"],
        "editable_paths": ["app.py"],
        "requirements": requirements,
        "required_checks": {"unit": "test"},
        "repair_attempts": repair_attempts,
        "delivery": {"mode": "local", "auto_merge": True},
    }
    if release:
        policy["release"] = {
            "deploy": {
                "id": "deploy",
                "kind": "command",
                "argv": ["{python}", "deploy.py", "{release_dir}", "{commit}"],
            },
            "health": {
                "id": "health",
                "kind": "command",
                "argv": ["{python}", "health.py", "{release_dir}"],
            },
            "rollback": {
                "id": "rollback",
                "kind": "command",
                "argv": ["{python}", "rollback.py", "{release_dir}"],
            },
        }
    return policy


def make_controller(
    root: Path,
    policies: list[dict],
    provider: "DeterministicTestDouble",
) -> Controller:
    fleet = {
        "schema_version": 1,
        "state_directory": "control-state",
        "repositories": policies,
    }
    fleet_path = root / "fleet.yaml"
    fleet_path.write_text(yaml.safe_dump(fleet), encoding="utf-8")
    return Controller(fleet_path, provider_factory=lambda _directory: provider)


class DeterministicTestDouble:
    """Deterministic CI injection at the model boundary, with no controller mocks."""

    def __init__(
        self,
        *,
        review_approved: bool = True,
        repair_path: str = "app.py",
        diagnosis_started: threading.Event | None = None,
    ) -> None:
        self.review_approved = review_approved
        self.repair_path = repair_path
        self.diagnosis_started = diagnosis_started
        self.roles: list[str] = []

    def ask(self, role, context, schema, cancel):
        self.roles.append(role)
        if role == "diagnosis":
            if self.diagnosis_started is not None:
                self.diagnosis_started.set()
                while not cancel.wait(0.01):
                    pass
                raise RuntimeError("test double observed cancellation")
            repairable = "unrepairable" not in context["standing_requirements"]
            return Diagnosis(
                summary="The seeded value violates the tracked verifier.",
                repairable=repairable,
                evidence=["unit:value_contract"],
            )
        if role == "repair":
            return Repair(
                summary="Set the implementation value required by the contract.",
                edits=[FileEdit(path=self.repair_path, content="VALUE = 2\n")],
            )
        if role == "review":
            return Review(
                approved=self.review_approved,
                summary="Verified candidate review.",
                concerns=[] if self.review_approved else ["Independent review rejected the patch."],
            )
        raise AssertionError(f"unexpected AI role: {role}")


class UnmanifestedMutationTestDouble(DeterministicTestDouble):
    def ask(self, role, context, schema, cancel):
        if role == "repair":
            workspace = Path(context["baseline"]["directory"]).parents[1] / "workspace"
            (workspace / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        return super().ask(role, context, schema, cancel)


class BaseMovingTestDouble(DeterministicTestDouble):
    def __init__(self, repository: Path) -> None:
        super().__init__()
        self.repository = repository

    def ask(self, role, context, schema, cancel):
        result = super().ask(role, context, schema, cancel)
        if role == "review":
            (self.repository / "notice.txt").write_text("new base\n", encoding="utf-8")
            git(self.repository, "add", "notice.txt")
            git(self.repository, "commit", "-m", "move base immediately before publication")
        return result


def test_rapid_same_size_repair_is_verified_reviewed_committed_published_and_merged(tmp_path):
    repository = make_repository(tmp_path, "service")
    assert len("VALUE = 1\n") == len("VALUE = 2\n")
    original = source_head(str(repository), "main")
    provider = DeterministicTestDouble()
    controller = make_controller(tmp_path, [repository_policy(repository)], provider)
    task = controller.enqueue("service", "seeded-repair")

    completed = controller.work_once()

    assert completed is not None
    assert completed["id"] == task["id"]
    assert completed["state"] == "completed"
    assert completed["result"]["outcome"] == "repaired"
    assert completed["result"]["baseline"]["gate"]["passed"] is False
    assert completed["result"]["baseline"]["results"][0]["status"] == "failed"
    assert completed["result"]["verification"]["gate"]["passed"] is True
    assert completed["result"]["verification"]["results"][0]["status"] == "passed"
    assert completed["result"]["review"]["approved"] is True
    assert provider.roles == ["diagnosis", "repair", "review"]
    assert source_head(str(repository), "main") != original
    assert (repository / "app.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    assert git(repository, "log", "-1", "--pretty=%s").startswith("fix: repair service")
    states = [event["state"] for event in controller.store.events(task["id"])]
    for state in (
        "testing",
        "diagnosing",
        "repairing",
        "verifying",
        "reviewing",
        "publishing",
        "completed",
    ):
        assert state in states


def test_failure_in_one_repository_does_not_stop_another(tmp_path):
    broken = make_repository(tmp_path, "broken")
    healthy = make_repository(tmp_path, "healthy")
    provider = DeterministicTestDouble()
    controller = make_controller(
        tmp_path,
        [
            repository_policy(broken, requirements="unrepairable outside dependency"),
            repository_policy(healthy),
        ],
        provider,
    )
    first = controller.enqueue("broken")
    second = controller.enqueue("healthy")

    assert controller.work_once()["state"] == "failed"
    assert controller.work_once()["state"] == "completed"
    assert controller.store.get_task(first["id"])["state"] == "failed"
    assert controller.store.get_task(second["id"])["state"] == "completed"
    assert (broken / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert (healthy / "app.py").read_text(encoding="utf-8") == "VALUE = 2\n"


def test_review_rejection_prevents_delivery(tmp_path):
    repository = make_repository(tmp_path, "service")
    original = source_head(str(repository), "main")
    provider = DeterministicTestDouble(review_approved=False)
    controller = make_controller(tmp_path, [repository_policy(repository)], provider)
    task = controller.enqueue("service")

    result = controller.work_once()

    assert result["state"] == "failed"
    assert "review" in result["result"]["error"].lower()
    assert source_head(str(repository), "main") == original
    assert (repository / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert "publishing" not in [event["state"] for event in controller.store.events(task["id"])]


def test_edit_outside_allowlist_blocks_candidate_and_delivery(tmp_path):
    repository = make_repository(tmp_path, "service")
    original = source_head(str(repository), "main")
    provider = DeterministicTestDouble(repair_path="verify.py")
    controller = make_controller(tmp_path, [repository_policy(repository)], provider)
    controller.enqueue("service")

    result = controller.work_once()

    assert result["state"] == "failed"
    assert "outside the allowed" in result["result"]["error"]
    assert source_head(str(repository), "main") == original
    assert (repository / "verify.py").read_text(encoding="utf-8") == VERIFY


def test_missing_junit_evidence_blocks_verified_repair_and_delivery(tmp_path):
    repository = make_repository(tmp_path, "service", missing_evidence=True)
    original = source_head(str(repository), "main")
    provider = DeterministicTestDouble()
    controller = make_controller(tmp_path, [repository_policy(repository)], provider)
    controller.enqueue("service")

    result = controller.work_once()

    assert result["state"] == "failed"
    assert result["result"]["verification"]["gate"]["passed"] is False
    verification = result["result"]["verification"]["results"][0]
    assert verification["status"] == "missing_evidence"
    assert source_head(str(repository), "main") == original


def test_durable_cancel_during_ai_fences_worker_and_blocks_publication(tmp_path):
    repository = make_repository(tmp_path, "service")
    original = source_head(str(repository), "main")
    diagnosis_started = threading.Event()
    provider = DeterministicTestDouble(diagnosis_started=diagnosis_started)
    controller = make_controller(tmp_path, [repository_policy(repository)], provider)
    task = controller.enqueue("service")
    results = []

    worker = threading.Thread(target=lambda: results.append(controller.work_once()))
    worker.start()
    assert diagnosis_started.wait(10)
    controlled = controller.store.control(task["id"], "cancel")
    worker.join(timeout=10)

    assert not worker.is_alive()
    assert controlled["state"] == "cancelled"
    assert results[0]["state"] == "cancelled"
    assert source_head(str(repository), "main") == original
    events = controller.store.events(task["id"])
    assert events[-1]["kind"] == "cancel"
    assert all(event["state"] != "publishing" for event in events)


def test_base_change_after_enqueue_rejects_stale_task(tmp_path):
    repository = make_repository(tmp_path, "service")
    provider = DeterministicTestDouble()
    controller = make_controller(tmp_path, [repository_policy(repository)], provider)
    task = controller.enqueue("service")
    (repository / "notice.txt").write_text("new base\n", encoding="utf-8")
    git(repository, "add", "notice.txt")
    git(repository, "commit", "-m", "move source after enqueue")
    moved = source_head(str(repository), "main")

    result = controller.work_once()

    assert result["id"] == task["id"]
    assert result["state"] == "failed"
    assert "Source head changed" in result["result"]["error"]
    assert source_head(str(repository), "main") == moved
    assert provider.roles == []


def test_unmanifested_ai_mutation_cannot_change_the_reviewed_commit(tmp_path):
    repository = make_repository(tmp_path, "service")
    (repository / "notes.py").write_text("NOTE = 'original'\n", encoding="utf-8")
    git(repository, "add", "notes.py")
    git(repository, "commit", "-m", "add second centrally editable file")
    original = source_head(str(repository), "main")
    provider = UnmanifestedMutationTestDouble(repair_path="notes.py")
    policy = repository_policy(repository)
    policy["context_paths"].append("notes.py")
    policy["editable_paths"].append("notes.py")
    controller = make_controller(tmp_path, [policy], provider)
    task = controller.enqueue("service")

    result = controller.work_once()

    assert result["state"] == "failed"
    assert "omitted reviewed changes" in result["result"]["error"]
    assert source_head(str(repository), "main") == original
    assert (repository / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert "publishing" not in [event["state"] for event in controller.store.events(task["id"])]


def test_worker_stop_before_and_during_early_work_requeues_for_restart(tmp_path):
    repository = make_repository(tmp_path, "service")
    diagnosis_started = threading.Event()
    provider = DeterministicTestDouble(diagnosis_started=diagnosis_started)
    controller = make_controller(tmp_path, [repository_policy(repository)], provider)
    task = controller.enqueue("service")
    stop = threading.Event()
    stop.set()

    assert controller.work_once(stop) is None
    untouched = controller.store.get_task(task["id"])
    assert untouched["state"] == "queued"
    assert untouched["lease"] is None
    assert untouched["epoch"] == 0

    stop.clear()
    results = []
    worker = threading.Thread(target=lambda: results.append(controller.work_once(stop)))
    worker.start()
    assert diagnosis_started.wait(10)
    stop.set()
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert results[0]["state"] == "queued"
    interrupted_epoch = results[0]["epoch"]

    replacement = DeterministicTestDouble()
    controller.provider_factory = lambda _directory: replacement
    completed = controller.work_once()
    assert completed["state"] == "completed"
    assert completed["epoch"] > interrupted_epoch
    assert replacement.roles == ["diagnosis", "repair", "review"]


def test_base_move_immediately_before_publication_is_known_failure_not_unknown_outcome(tmp_path):
    repository = make_repository(tmp_path, "service")
    provider = BaseMovingTestDouble(repository)
    controller = make_controller(tmp_path, [repository_policy(repository)], provider)
    task = controller.enqueue("service")

    result = controller.work_once()

    assert result["state"] == "failed"
    assert "Base changed before publication" in result["result"]["error"]
    assert result["result"].get("reconciliation_required") is not True
    assert "publishing" not in [event["state"] for event in controller.store.events(task["id"])]


def test_failed_release_runs_real_rollback_and_post_rollback_health(tmp_path):
    repository = make_repository(tmp_path, "service", release_scripts=True)
    provider = DeterministicTestDouble()
    controller = make_controller(
        tmp_path,
        [repository_policy(repository, release=True)],
        provider,
    )
    task = controller.enqueue("service")

    result = controller.work_once()

    assert result["state"] == "failed"
    assert result["result"]["deploy"]["gate"]["passed"] is True
    assert result["result"]["health"]["gate"]["passed"] is False
    assert result["result"]["rollback"]["gate"]["passed"] is True
    assert result["result"]["rollback_health"]["gate"]["passed"] is True
    release = tmp_path / "control-state" / "releases" / "service"
    assert (release / "deployed.txt").read_text(encoding="utf-8") == "old"
    assert not (release / "last-success.json").exists()
    assert (repository / "app.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    states = [
        event["state"]
        for event in controller.store.events(task["id"])
        if event["kind"] == "transition"
    ]
    assert states.count("deploying") == 2
    assert states.count("monitoring") == 2


ENVIRONMENT_VERIFY = """import importlib
import os
import sys
from pathlib import Path

application = importlib.import_module("app")
environment = Path(os.environ["KATYDID_ENVIRONMENT_DIR"])
try:
    assert environment.resolve() == Path(sys.argv[1]).resolve()
    assert (environment / "data.txt").read_text(encoding="utf-8") == "run-owned data"
    assert application.VALUE == 2, f"expected VALUE=2, got {application.VALUE!r}"
except Exception as exc:
    body = f'<testcase name="environment_contract"><failure>{exc}</failure></testcase>'
    code = 1
else:
    body = '<testcase name="environment_contract"/>'
    code = 0
report = Path(os.environ["KATYDID_REPORT_PATH"])
report.write_text(f'<testsuite tests="1" failures="{code}">{body}</testsuite>', encoding="utf-8")
raise SystemExit(code)
"""


ENVIRONMENT_LIFECYCLE = """import json
import os
import sys
from pathlib import Path

action, environment_value, log_value, counter_value, scenario, run_id = sys.argv[1:]
environment = Path(environment_value)
log = Path(log_value)
counter = Path(counter_value)
count = int(counter.read_text(encoding="utf-8")) if counter.exists() else 0
if action == "prepare":
    count += 1
    counter.write_text(str(count), encoding="utf-8")
    (environment / "data.txt").write_text("run-owned data", encoding="utf-8")

def record(**details):
    with log.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({
            "action": action,
            "environment": str(environment.resolve()),
            "run_id": run_id,
            "environment_run_id": os.environ["KATYDID_RUN_ID"],
            "count": count,
            **details,
        }) + "\\n")

if action == "prepare":
    record(data_exists=(environment / "data.txt").exists())
    if scenario == "initial-prepare-fail":
        raise SystemExit(3)
elif action == "readiness":
    record(data_exists=(environment / "data.txt").exists())
    if scenario == "verification-readiness-fail" and count >= 2:
        raise SystemExit(4)
    if not (environment / "data.txt").exists():
        raise SystemExit(5)
elif action == "cleanup":
    (environment / "data.txt").unlink(missing_ok=True)
    record(data_exists=(environment / "data.txt").exists())
    if scenario == "baseline-cleanup-fail" or (
        scenario == "verification-cleanup-fail" and count >= 2
    ):
        raise SystemExit(6)
else:
    raise SystemExit(7)
"""


def add_environment_lifecycle(repository: Path, scenario: str) -> tuple[Path, Path]:
    log = repository.parent / f"{repository.name}-{scenario}-lifecycle.jsonl"
    counter = repository.parent / f"{repository.name}-{scenario}-counter.txt"
    (repository / "verify.py").write_text(ENVIRONMENT_VERIFY, encoding="utf-8")
    (repository / "environment.py").write_text(ENVIRONMENT_LIFECYCLE, encoding="utf-8")
    profile_path = repository / "quality.yaml"
    profile = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    profile["checks"][0]["argv"].append("{environment}")

    def hook(action: str) -> dict:
        return {
            "id": f"environment-{action}",
            "kind": "command",
            "argv": [
                "{python}",
                "environment.py",
                action,
                "{environment}",
                str(log),
                str(counter),
                scenario,
                "{run_id}",
            ],
            "timeout_seconds": 10,
            "required": True,
        }

    profile["environment"] = {
        "prepare": [hook("prepare")],
        "readiness": {
            "check": hook("readiness"),
            "timeout_seconds": 1,
            "interval_seconds": 0.05,
        },
        "cleanup": [hook("cleanup")],
    }
    profile_path.write_text(yaml.safe_dump(profile), encoding="utf-8")
    git(repository, "add", ".")
    git(repository, "commit", "-m", f"test: add {scenario} environment lifecycle")
    return log, counter


def lifecycle_records(log: Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def assert_environment_cleanup(records: list[dict], expected_runs: int) -> None:
    prepares = [record for record in records if record["action"] == "prepare"]
    cleanups = [record for record in records if record["action"] == "cleanup"]
    assert len(prepares) == expected_runs
    assert len(cleanups) == expected_runs
    environments = {record["environment"] for record in prepares}
    assert len(environments) == expected_runs
    assert environments == {record["environment"] for record in cleanups}
    assert all(record["run_id"] == record["environment_run_id"] for record in records)
    assert all(record["data_exists"] is False for record in cleanups)
    assert all(not (Path(environment) / "data.txt").exists() for environment in environments)


def test_repair_baseline_and_verification_use_fresh_cleaned_environments(tmp_path):
    repository = make_repository(tmp_path, "service")
    log, _counter = add_environment_lifecycle(repository, "success")
    provider = DeterministicTestDouble()
    controller = make_controller(tmp_path, [repository_policy(repository)], provider)
    task = controller.enqueue("service")

    result = controller.work_once()

    assert result["state"] == "completed"
    assert result["result"]["outcome"] == "repaired"
    assert result["result"]["baseline"]["environment"]["ready"] is True
    assert result["result"]["baseline"]["environment"]["cleanup_complete"] is True
    assert result["result"]["verification"]["environment"]["ready"] is True
    assert result["result"]["verification"]["environment"]["cleanup_complete"] is True
    assert provider.roles == ["diagnosis", "repair", "review"]
    records = lifecycle_records(log)
    assert_environment_cleanup(records, 2)
    workspace = Path(result["result"]["workspace"])
    assert all(not Path(record["environment"]).is_relative_to(workspace) for record in records)
    assert "publishing" in [event["state"] for event in controller.store.events(task["id"])]


def test_initial_environment_setup_failure_stops_before_ai_and_cleans_up(tmp_path):
    repository = make_repository(tmp_path, "service")
    log, _counter = add_environment_lifecycle(repository, "initial-prepare-fail")
    original = source_head(str(repository), "main")
    provider = DeterministicTestDouble()
    controller = make_controller(tmp_path, [repository_policy(repository)], provider)
    task = controller.enqueue("service")

    result = controller.work_once()

    assert result["state"] == "failed"
    assert "Environment lifecycle failed" in result["result"]["error"]
    assert provider.roles == []
    assert source_head(str(repository), "main") == original
    assert "publishing" not in [event["state"] for event in controller.store.events(task["id"])]
    assert_environment_cleanup(lifecycle_records(log), 1)


def test_baseline_cleanup_failure_stops_before_ai_and_delivery(tmp_path):
    repository = make_repository(tmp_path, "service")
    log, _counter = add_environment_lifecycle(repository, "baseline-cleanup-fail")
    original = source_head(str(repository), "main")
    provider = DeterministicTestDouble()
    controller = make_controller(tmp_path, [repository_policy(repository)], provider)
    task = controller.enqueue("service")

    result = controller.work_once()

    assert result["state"] == "failed"
    assert "Environment lifecycle failed" in result["result"]["error"]
    assert result["result"]["baseline"]["environment"]["ready"] is True
    assert result["result"]["baseline"]["environment"]["cleanup_complete"] is False
    assert provider.roles == []
    assert source_head(str(repository), "main") == original
    assert "publishing" not in [event["state"] for event in controller.store.events(task["id"])]
    assert_environment_cleanup(lifecycle_records(log), 1)


@pytest.mark.parametrize("scenario", ["verification-readiness-fail", "verification-cleanup-fail"])
def test_verification_environment_failure_stops_without_more_ai_or_delivery(tmp_path, scenario):
    repository = make_repository(tmp_path, "service")
    log, _counter = add_environment_lifecycle(repository, scenario)
    original = source_head(str(repository), "main")
    provider = DeterministicTestDouble()
    policy = repository_policy(repository, repair_attempts=2)
    controller = make_controller(tmp_path, [policy], provider)
    task = controller.enqueue("service")

    result = controller.work_once()

    assert result["state"] == "failed"
    assert "Environment lifecycle failed" in result["result"]["error"]
    assert result["result"]["baseline"]["environment"]["ready"] is True
    assert result["result"]["baseline"]["environment"]["cleanup_complete"] is True
    verification = result["result"]["verification"]["environment"]
    if scenario == "verification-readiness-fail":
        assert verification["ready"] is False
        assert verification["cleanup_complete"] is True
    else:
        assert verification["ready"] is True
        assert verification["cleanup_complete"] is False
    assert provider.roles == ["diagnosis", "repair"]
    assert source_head(str(repository), "main") == original
    assert "publishing" not in [event["state"] for event in controller.store.events(task["id"])]
    assert_environment_cleanup(lifecycle_records(log), 2)
