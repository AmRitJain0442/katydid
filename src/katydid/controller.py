"""Single-host autonomous testing, repair, delivery, and release orchestration."""

import hashlib
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

from katydid.ai import AIProvider, CodexProvider, Diagnosis, Repair, Response, Review
from katydid.fleet import RepositoryConfig, enforce_policy, load_fleet
from katydid.profile import Check, Plan, PlannedCheck, ProfileError, Stage, make_plan
from katydid.runner import Run, _write_json, run_plan
from katydid.store import Lease, LeaseConflict, StaleLease, Store
from katydid.tasks import TaskRequest
from katydid.workspace import (
    GitWorkspace,
    apply_edits,
    commit,
    diff,
    merge_local,
    merge_pull_request,
    open_pull_request,
    prepare_workspace,
    publish_local,
    snapshot_files,
    source_head,
    source_revision,
)


class ControllerError(RuntimeError):
    pass


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    ).stdout.strip()


def _evidence(run: Run) -> dict[str, Any]:
    logs = {}
    for path in sorted(run.directory.glob("*/*.log")):
        with path.open("rb") as stream:
            stream.seek(max(0, path.stat().st_size - 6000))
            logs[str(path.relative_to(run.directory))] = stream.read(6000).decode(
                "utf-8", "replace"
            )
    return {
        "directory": str(run.directory),
        "gate": asdict(run.gate),
        "results": [asdict(result) for result in run.results],
        "log_tails": logs,
        "environment": asdict(run.environment) if run.environment else None,
        "isolation": run.isolation,
    }


class Controller:
    def __init__(
        self,
        fleet_path: Path,
        provider_factory: Callable[[Path], AIProvider] | None = None,
    ) -> None:
        self.fleet_path = fleet_path.resolve()
        self.config, self.digest = load_fleet(self.fleet_path)
        self.directory = Path(self.config.state_directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.directory / "state.db")
        self.provider_factory = provider_factory or (
            lambda directory: CodexProvider(self.config.ai, directory)
        )

    def enqueue(
        self,
        repository: str,
        idempotency_key: str | None = None,
        *,
        stage: Stage = "pull-request",
        mode: Literal["check", "repair", "release"] = "repair",
    ) -> dict[str, Any]:
        repo = self.config.repository(repository)
        self._unchanged_policy()
        head = source_head(repo.source, repo.base_branch)
        request = TaskRequest(config_sha256=self.digest, base_sha=head, stage=stage, mode=mode)
        self._request_policy(repo, request)
        return self.store.create_task(repository, request.model_dump(), idempotency_key)

    def discover(self, *, period: int | None = None) -> list[dict[str, Any]]:
        """Queue changed heads for merge checks, or unchanged heads for nightly checks."""
        tasks = []
        self._unchanged_policy()
        for repo in self.config.repositories:
            head = source_head(repo.source, repo.base_branch)
            stage: Stage = "merge" if period is None else "nightly"
            key = f"discover:{repo.id}:{head}:{self.digest}:{stage}:{period}"
            request = TaskRequest(
                config_sha256=self.digest, base_sha=head, stage=stage, mode="check"
            )
            tasks.append(self.store.create_task(repo.id, request.model_dump(), key))
        return tasks

    def _unchanged_policy(self) -> None:
        if hashlib.sha256(self.fleet_path.read_bytes()).hexdigest() != self.digest:
            raise ControllerError("Central policy changed; restart the controller before new work")

    def work_once(self, stop: threading.Event | None = None) -> dict[str, Any] | None:
        if stop is not None and stop.is_set():
            return None
        self.store.recover_expired()
        for task in self.store.list_tasks():
            if task["state"] != "queued":
                continue
            try:
                lease = self.store.claim(task["id"], f"worker-{uuid.uuid4().hex}")
            except LeaseConflict:
                continue
            return self.execute(lease, stop)
        return None

    def execute(self, lease: Lease, stop: threading.Event | None = None) -> dict[str, Any]:
        cancelled = threading.Event()
        finished = threading.Event()
        task = self.store.get_task(lease.task_id)
        evidence: dict[str, Any] = {"task_id": lease.task_id, "epoch": lease.epoch}
        folder = self.directory / "tasks" / lease.task_id / str(lease.epoch)
        folder.mkdir(parents=True, exist_ok=False)

        def active() -> None:
            self.store.assert_active(lease)
            self._unchanged_policy()
            if stop is not None and stop.is_set():
                raise ControllerError("Worker shutdown requested")

        def heartbeat() -> None:
            renewed = time.monotonic()
            while not finished.wait(0.2):
                try:
                    active()
                    if time.monotonic() - renewed >= 5:
                        self.store.renew(lease)
                        renewed = time.monotonic()
                except Exception:
                    cancelled.set()
                    return

        def state(name: str, **details: Any) -> None:
            active()
            self.store.transition(lease, name, details)

        thread = threading.Thread(target=heartbeat, daemon=True, name="katydid-lease")
        thread.start()
        try:
            active()
            request = TaskRequest.model_validate(task["payload"])
            if request.config_sha256 != self.digest:
                raise ControllerError("Queued task belongs to a different central policy")
            repo = self.config.repository(task["repository"])
            self._request_policy(repo, request)

            def fresh() -> None:
                active()
                if source_head(repo.source, repo.base_branch) != request.base_sha:
                    raise ControllerError("Source head changed after enqueue; submit a fresh task")
                if request.source_ref and source_revision(repo.source, request.source_ref) != (
                    request.source_sha
                ):
                    raise ControllerError("Source event revision is stale; submit a fresh task")

            fresh()
            state("preparing")
            workspace = prepare_workspace(
                repo.source,
                folder / "workspace",
                repo.base_branch,
                f"katydid/{repo.id}/{lease.task_id[:12]}-{lease.epoch}",
                source_ref=request.source_ref,
                source_sha=request.source_sha,
            )
            evidence.update(
                workspace=str(workspace.path),
                base_sha=workspace.base_sha,
                branch=workspace.branch,
                config_sha256=self.digest,
                stage=request.stage,
                mode=request.mode,
                source_sha=_git(workspace.path, "rev-parse", "HEAD"),
                source_ref=request.source_ref or f"refs/heads/{repo.base_branch}",
                event=request.event,
            )
            if workspace.base_sha != task["payload"].get("base_sha"):
                raise ControllerError("Source head changed after enqueue; submit a fresh task")
            if request.source_ref and request.source_ref.startswith("refs/pull/"):
                protected_paths = sorted(
                    {repo.profile, *set(repo.context_paths).difference(repo.editable_paths)}
                )
                if _git(
                    workspace.path,
                    "diff",
                    "--name-only",
                    request.base_sha,
                    evidence["source_sha"],
                    "--",
                    *protected_paths,
                ):
                    raise ControllerError(
                        "Pull request changed centrally protected checks or profile"
                    )
            plan = make_plan(workspace.path / repo.profile, request.stage, workspace.path)
            enforce_policy(repo, plan)
            original = snapshot_files(
                workspace.path, repo.context_paths, self.config.ai.max_context_bytes
            )
            protected = {
                path: content
                for path, content in original.items()
                if path not in repo.editable_paths
            }
            state("testing", profile_sha256=plan.profile_sha256)
            baseline = run_plan(plan, folder / "runs", cancelled)
            active()
            evidence["baseline"] = _evidence(baseline)
            self._ensure_environment(baseline)
            self._assert_files(workspace, repo, protected)
            if diff(workspace):
                raise ControllerError("Baseline checks modified tracked files")
            if baseline.gate.passed:
                fresh()
                if request.mode == "release":
                    self._release(
                        repo,
                        workspace,
                        evidence["source_sha"],
                        folder,
                        cancelled,
                        state,
                        evidence,
                        before_hook=fresh,
                    )
                    state("completed", **evidence, outcome="released", ai_calls=0)
                    return self.store.get_task(lease.task_id)
                state("completed", **evidence, outcome="healthy", ai_calls=0)
                return self.store.get_task(lease.task_id)
            if request.mode != "repair":
                raise ControllerError(
                    "Required stage checks failed; task does not permit AI repair"
                )
            if not repo.editable_paths:
                raise ControllerError("Checks failed and central policy grants no editable files")
            provider = self.provider_factory(folder / "ai")

            def ask(role: str, data: dict[str, Any], schema: type[Response]) -> Response:
                active()
                self.store.reserve_ai_call(lease, role, self.config.ai.max_calls_per_task)
                return provider.ask(role, data, schema, cancelled)

            context: dict[str, Any] = {
                "standing_requirements": repo.requirements,
                "operator_instructions": task["instructions"],
                "editable_paths": repo.editable_paths,
                "files": original,
                "baseline": evidence["baseline"],
            }
            state("diagnosing")
            diagnosis = ask("diagnosis", context, Diagnosis)
            active()
            evidence["diagnosis"] = diagnosis.model_dump()
            context["diagnosis"] = diagnosis.model_dump()
            if not diagnosis.repairable:
                raise ControllerError(
                    f"AI diagnosis requires unresolved infrastructure or scope: {diagnosis.summary}"
                )
            accepted = False
            for attempt in range(1, repo.repair_attempts + 1):
                state("repairing", attempt=attempt)
                context["files"] = snapshot_files(
                    workspace.path, repo.context_paths, self.config.ai.max_context_bytes
                )
                proposal = ask("repair", context, Repair)
                active()
                apply_edits(
                    workspace, [edit.model_dump() for edit in proposal.edits], repo.editable_paths
                )
                self._assert_files(workspace, repo, protected)
                candidate_diff = diff(workspace)
                if not candidate_diff:
                    raise ControllerError("AI proposal made no implementation change")
                state("verifying", attempt=attempt)
                verified = run_plan(plan, folder / "runs", cancelled)
                active()
                evidence["verification"] = _evidence(verified)
                self._ensure_environment(verified)
                self._assert_files(workspace, repo, protected)
                if diff(workspace) != candidate_diff:
                    raise ControllerError("Verification changed the candidate implementation")
                context["verification"] = evidence["verification"]
                context["diff"] = candidate_diff
                context["files"] = snapshot_files(
                    workspace.path, repo.context_paths, self.config.ai.max_context_bytes
                )
                if not verified.gate.passed:
                    continue
                state("reviewing", attempt=attempt)
                # Review uses a fresh call with evidence and diff, without author rationale.
                review_context = {
                    key: value for key, value in context.items() if key != "diagnosis"
                }
                review = ask("review", review_context, Review)
                active()
                evidence["review"] = review.model_dump()
                if review.approved and not review.concerns:
                    accepted = True
                    break
                context["review_feedback"] = review.model_dump()
            if not accepted:
                raise ControllerError(
                    "Repair attempts exhausted without passing tests and AI review"
                )
            self._assert_files(workspace, repo, protected)
            if diff(workspace) != candidate_diff:
                raise ControllerError("Candidate changed after verification")
            if repo.release is not None:
                release_plan = make_plan(workspace.path / repo.profile, "release", workspace.path)
                enforce_policy(repo, release_plan)
                state("verifying", stage="release")
                release_verified = run_plan(release_plan, folder / "runs", cancelled)
                active()
                evidence["release_verification"] = _evidence(release_verified)
                self._ensure_environment(release_verified)
                self._assert_files(workspace, repo, protected)
                if not release_verified.gate.passed or diff(workspace) != candidate_diff:
                    raise ControllerError(
                        "Release-stage candidate verification failed before delivery"
                    )
            active()
            sha = commit(workspace, f"fix: repair {repo.id} after verified AI review")
            if diff(workspace):
                raise ControllerError("Commit omitted reviewed changes; refusing delivery")
            committed_diff = _git(
                workspace.path, "diff", "--binary", "--no-ext-diff", workspace.base_sha, sha, "--"
            )
            if committed_diff != candidate_diff.strip():
                raise ControllerError("Committed change differs from the reviewed candidate")
            evidence["candidate_sha"] = sha
            evidence["candidate_tree"] = _git(workspace.path, "rev-parse", "HEAD^{tree}")
            _write_json(folder / "candidate.json", evidence)
            if repo.delivery.mode != "none":
                active()
                if source_head(repo.source, repo.base_branch) != workspace.base_sha:
                    raise ControllerError(
                        "Base changed before publication; candidate needs fresh testing"
                    )
                state("publishing", candidate_sha=sha)
                if repo.delivery.mode == "local":
                    evidence["published_sha"] = publish_local(workspace)
                    if repo.delivery.auto_merge:
                        active()
                        evidence["merged_sha"] = merge_local(
                            repo.source, workspace.branch, workspace.base_sha, repo.base_branch
                        )
                else:
                    from katydid.workspace import wait_pull_request

                    github = repo.delivery.github_repository
                    if github is None:
                        raise ControllerError("Missing GitHub repository")
                    body = (
                        f"Automated repair for `{repo.id}`. Protected checks failed at "
                        f"`{workspace.base_sha}` and passed for candidate `{sha}`.\n\n"
                        f"Independent AI review: {review.summary}\n\n"
                        f"Task: `{lease.task_id}`. Evidence is retained by the Katydid host."
                    )
                    pr = open_pull_request(
                        workspace,
                        github,
                        repo.base_branch,
                        f"fix: repair {repo.id} with verified evidence",
                        body,
                    )
                    evidence["pull_request"] = pr
                    if repo.delivery.auto_merge:
                        evidence["github_checks"] = wait_pull_request(
                            github, pr["number"], sha, cancelled
                        )
                        active()
                        if source_head(repo.source, repo.base_branch) != workspace.base_sha:
                            raise ControllerError("Base changed while awaiting GitHub checks")
                        merged = merge_pull_request(github, pr["number"], sha)
                        evidence["merged_sha"] = merged["mergeCommit"]["oid"]
                active()
            if repo.release is not None:
                merged_sha = evidence["merged_sha"]
                _git(workspace.path, "fetch", "origin", repo.base_branch)
                actual_tree = _git(workspace.path, "rev-parse", f"{merged_sha}^{{tree}}")
                if actual_tree != evidence["candidate_tree"]:
                    raise ControllerError(
                        "Merged tree differs from the tested candidate; refusing release"
                    )
                self._release(repo, workspace, merged_sha, folder, cancelled, state, evidence)
            state("completed", **evidence, outcome="repaired")
        except Exception as exc:
            evidence["error"] = str(exc)
            current = self.store.get_task(lease.task_id)
            try:
                self.store.assert_active(lease)
                external = current["state"] in ("publishing", "deploying", "monitoring")
                if stop is not None and stop.is_set() and not external:
                    self.store.release(lease)
                else:
                    self.store.transition(lease, "unresolved" if external else "failed", evidence)
            except StaleLease:
                pass  # The operator's newer epoch owns the state; never overwrite it.
        finally:
            finished.set()
            cancelled.set()
            thread.join(timeout=2)
            _write_json(folder / "outcome.json", evidence)
        return self.store.get_task(lease.task_id)

    @staticmethod
    def _request_policy(repo: RepositoryConfig, request: TaskRequest) -> None:
        if request.source_ref and request.source_ref.startswith("refs/pull/"):
            if repo.isolation_policy is None or not repo.isolation_policy.required:
                raise ControllerError(
                    "Pull-request events require centrally enforced Docker isolation"
                )
        if request.mode == "repair" and request.source_sha not in (None, request.base_sha):
            raise ControllerError("Repair must start from the registered base revision")
        if request.mode == "release":
            if repo.release is None:
                raise ControllerError("Release task requires centrally configured release hooks")
            if request.source_sha not in (None, request.base_sha):
                raise ControllerError("Release must validate the current registered base revision")

    @staticmethod
    def _ensure_environment(run: Run) -> None:
        if run.isolation is not None and (
            not run.isolation["ready"]
            or not run.isolation["cleanup_complete"]
            or run.isolation["errors"]
        ):
            raise ControllerError("Docker isolation failed; refusing application AI repair")
        if run.environment is not None and (
            not run.environment.ready or not run.environment.cleanup_complete
        ):
            raise ControllerError(
                "Environment lifecycle failed; infrastructure requires attention before AI repair"
            )

    @staticmethod
    def _assert_files(
        workspace: GitWorkspace, repo: RepositoryConfig, protected: dict[str, str]
    ) -> None:
        if snapshot_files(workspace.path, list(protected)) != protected:
            raise ControllerError("Protected context files changed")
        changed = _git(workspace.path, "diff", "--name-only", "HEAD", "--").splitlines()
        if not set(changed).issubset(repo.editable_paths):
            raise ControllerError("Changes escaped the central editable file list")

    def _release(
        self,
        repo: RepositoryConfig,
        workspace: GitWorkspace,
        sha: str,
        folder: Path,
        cancelled: threading.Event,
        state: Callable[..., None],
        evidence: dict[str, Any],
        before_hook: Callable[[], None] | None = None,
    ) -> None:
        release = repo.release
        assert release is not None
        release_directory = self.directory / "releases" / repo.id
        release_directory.mkdir(parents=True, exist_ok=True)

        def hook(check: Check) -> Run:
            if before_hook is not None:
                before_hook()
            cwd = (workspace.path / check.working_directory).resolve()
            if not cwd.is_relative_to(workspace.path) or not cwd.is_dir():
                raise ProfileError("Release working directory must exist inside the workspace")
            replacements = {
                "{workspace}": str(workspace.path),
                "{commit}": sha,
                "{release_dir}": str(release_directory),
            }
            argv = []
            for arg in check.argv:
                for key, value in replacements.items():
                    arg = arg.replace(key, value)
                argv.append(arg)
            plan = Plan(
                1,
                repo.id,
                "central-release-policy",
                "release",
                str(workspace.path),
                str(self.fleet_path),
                self.digest,
                (
                    PlannedCheck(
                        check.id, check.kind, tuple(argv), str(cwd), check.timeout_seconds, True
                    ),
                ),
                (),
            )
            return run_plan(plan, folder / "release-runs", cancelled)

        state("deploying", merged_sha=sha)
        deploy = hook(release.deploy)
        evidence["deploy"] = _evidence(deploy)
        state("monitoring", merged_sha=sha)
        health = hook(release.health) if deploy.gate.passed else None
        evidence["health"] = _evidence(health) if health else None
        if health is not None and health.gate.passed:
            _write_json(
                release_directory / "last-success.json",
                {"commit": sha, "task": evidence["task_id"]},
            )
            return
        # Rollback remains subject to the current control epoch, including operator interruption.
        state("deploying", operation="rollback", merged_sha=sha)
        rollback = hook(release.rollback)
        evidence["rollback"] = _evidence(rollback)
        state("monitoring", operation="rollback-health")
        recovered = hook(release.health) if rollback.gate.passed else None
        evidence["rollback_health"] = _evidence(recovered) if recovered else None
        if recovered and recovered.gate.passed:
            state(
                "failed",
                **evidence,
                error="Release failed; previous deployment restored and healthy",
            )
            raise ControllerError("Release failed; rollback succeeded")
        raise ControllerError(
            "Release and recovery did not pass health checks; reconciliation required"
        )
