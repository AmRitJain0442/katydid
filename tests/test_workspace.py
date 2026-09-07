import json
import os
import subprocess
import threading
from pathlib import Path

import pytest

import katydid.workspace as workspace_module
from katydid.workspace import (
    GitWorkspace,
    WorkspaceError,
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
    tree_sha,
    wait_pull_request,
)


def git(repository: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


def repository(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    root.mkdir()
    git(root, "init", "--initial-branch=main")
    git(root, "config", "user.name", "Katydid Test")
    git(root, "config", "user.email", "katydid@example.invalid")
    (root / "app.txt").write_text("base\n", encoding="utf-8")
    (root / "script.sh").write_text("#!/bin/sh\necho base\n", encoding="utf-8")
    if os.name != "nt":
        (root / "script.sh").chmod(0o755)
    git(root, "add", "app.txt", "script.sh")
    git(root, "commit", "-m", "initial")
    return root


def configure_identity(workspace: GitWorkspace) -> None:
    git(workspace.path, "config", "user.name", "Katydid Test")
    git(workspace.path, "config", "user.email", "katydid@example.invalid")


def test_prepare_is_an_independent_clean_clone_and_does_not_mutate_source(tmp_path: Path):
    source = repository(tmp_path)
    original = source_head(str(source), "main")
    clone = prepare_workspace(str(source), tmp_path / "workspace", "main", "candidate")

    assert clone.base_sha == original
    assert clone.path == tmp_path / "workspace"
    assert git(clone.path, "branch", "--show-current") == "candidate"
    assert git(clone.path, "status", "--porcelain") == ""
    assert not (clone.path / ".git" / "objects" / "info" / "alternates").exists()
    assert git(clone.path, "config", "--local", "user.name") == "Katydid Automation"
    assert git(clone.path, "config", "--local", "user.email") == (
        "katydid@users.noreply.github.com"
    )
    assert Path(git(clone.path, "config", "--local", "core.hooksPath")).is_dir()

    (clone.path / "app.txt").write_text("clone only\n", encoding="utf-8")
    assert (source / "app.txt").read_text(encoding="utf-8") == "base\n"
    assert source_head(str(source), "main") == original
    assert git(source, "status", "--porcelain") == ""


def test_prepare_rejects_existing_or_source_nested_destination(tmp_path: Path):
    source = repository(tmp_path)
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(WorkspaceError, match="must not already exist"):
        prepare_workspace(str(source), existing, "main", "candidate")
    with pytest.raises(WorkspaceError, match="inside the source"):
        prepare_workspace(str(source), source / "workspace", "main", "candidate")


def test_snapshot_is_exact_bounded_utf8_and_rejects_secrets(tmp_path: Path):
    source = repository(tmp_path)
    (source / "notes.txt").write_text("hello", encoding="utf-8")
    (source / ".env.local").write_text("TOKEN=secret", encoding="utf-8")
    assert snapshot_files(source, ["notes.txt", "app.txt"]) == {
        "notes.txt": "hello",
        "app.txt": (source / "app.txt").read_bytes().decode("utf-8"),
    }
    with pytest.raises(WorkspaceError, match="byte limit"):
        snapshot_files(source, ["notes.txt"], max_bytes=4)
    with pytest.raises(WorkspaceError, match="secret"):
        snapshot_files(source, [".env.local"])
    assert snapshot_files(source, ["generated.py"]) == {}


@pytest.mark.parametrize(
    "path",
    [
        "../outside.txt",
        "/absolute.txt",
        "folder/../outside.txt",
        ".git/config",
        "folder\\windows.txt",
        "C:/windows.txt",
        ".env",
        "config/.env.production",
    ],
)
def test_snapshot_rejects_nonliteral_sensitive_and_git_paths(tmp_path: Path, path: str):
    source = repository(tmp_path)
    with pytest.raises(WorkspaceError):
        snapshot_files(source, [path])


def test_snapshot_and_edits_reject_symlinks(tmp_path: Path):
    source = repository(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    link = source / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("Creating symlinks is not permitted on this host")
    with pytest.raises(WorkspaceError, match="Symlink"):
        snapshot_files(source, ["link.txt"])
    clone = prepare_workspace(str(source), tmp_path / "workspace", "main", "candidate")
    (clone.path / "link.txt").symlink_to(outside)
    with pytest.raises(WorkspaceError, match="Symlink"):
        apply_edits(clone, [{"path": "link.txt", "content": "changed"}], ["link.txt"])
    assert outside.read_text(encoding="utf-8") == "outside"


def test_apply_validates_entire_batch_before_writing_and_preserves_mode(tmp_path: Path):
    source = repository(tmp_path)
    clone = prepare_workspace(str(source), tmp_path / "workspace", "main", "candidate")
    original_mode = (clone.path / "script.sh").stat().st_mode
    edits = [
        {"path": "app.txt", "content": "would change\n"},
        {"path": "not-allowed.txt", "content": "bad\n"},
    ]
    with pytest.raises(WorkspaceError, match="outside the allowed"):
        apply_edits(clone, edits, ["app.txt"])
    assert (clone.path / "app.txt").read_text(encoding="utf-8") == "base\n"

    changed = apply_edits(
        clone,
        [
            {"path": "script.sh", "content": "#!/bin/sh\necho changed\n"},
            {"path": "generated.py", "content": "VALUE = 1\n"},
        ],
        ["script.sh", "generated.py"],
    )
    assert changed == ["script.sh", "generated.py"]
    assert (clone.path / "script.sh").stat().st_mode == original_mode
    patch = diff(clone)
    assert "echo changed" in patch
    assert "VALUE = 1" in patch


def test_apply_rejects_duplicates_and_total_byte_limit(tmp_path: Path, monkeypatch):
    source = repository(tmp_path)
    clone = prepare_workspace(str(source), tmp_path / "workspace", "main", "candidate")
    duplicate = [
        {"path": "app.txt", "content": "one"},
        {"path": "app.txt", "content": "two"},
    ]
    with pytest.raises(WorkspaceError, match="Duplicate"):
        apply_edits(clone, duplicate, ["app.txt"])
    monkeypatch.setattr(workspace_module, "MAX_EDIT_BYTES", 3)
    with pytest.raises(WorkspaceError, match="byte limit"):
        apply_edits(clone, [{"path": "app.txt", "content": "four"}], ["app.txt"])
    assert (clone.path / "app.txt").read_text(encoding="utf-8") == "base\n"


def test_commit_includes_only_applied_paths_and_treats_shell_text_literally(tmp_path: Path):
    source = repository(tmp_path)
    marker = tmp_path / "must-not-exist"
    clone = prepare_workspace(str(source), tmp_path / "workspace", "main", "candidate")
    configure_identity(clone)
    literal = "literal;$(touch must-not-exist).txt"
    apply_edits(
        clone,
        [
            {"path": "app.txt", "content": "changed\n"},
            {"path": literal, "content": "safe\n"},
        ],
        ["app.txt", literal],
    )
    (clone.path / "unrelated.txt").write_text("do not stage\n", encoding="utf-8")
    sha = commit(clone, "literal; $(touch must-not-exist)")

    assert sha == git(clone.path, "rev-parse", "HEAD")
    assert git(clone.path, "show", "--format=", "--name-only", "HEAD").splitlines() == [
        "app.txt",
        literal,
    ]
    assert "unrelated.txt" in git(clone.path, "status", "--porcelain")
    assert not marker.exists()


def test_local_publish_and_checked_out_fast_forward_merge(tmp_path: Path):
    source = repository(tmp_path)
    clone = prepare_workspace(str(source), tmp_path / "workspace", "main", "candidate")
    configure_identity(clone)
    apply_edits(clone, [{"path": "app.txt", "content": "delivered\n"}], ["app.txt"])
    delivered = commit(clone, "deliver")

    assert publish_local(clone) == delivered
    assert source_head(str(source), "main") == clone.base_sha
    assert merge_local(str(source), "candidate", clone.base_sha, "main") == delivered
    assert source_head(str(source), "main") == delivered
    assert (source / "app.txt").read_text(encoding="utf-8") == "delivered\n"
    assert tree_sha(str(source), delivered) == git(source, "rev-parse", f"{delivered}^{{tree}}")


def test_merge_rejects_stale_base_without_changing_source(tmp_path: Path):
    source = repository(tmp_path)
    clone = prepare_workspace(str(source), tmp_path / "workspace", "main", "candidate")
    configure_identity(clone)
    apply_edits(clone, [{"path": "app.txt", "content": "candidate\n"}], ["app.txt"])
    commit(clone, "candidate")
    publish_local(clone)

    (source / "app.txt").write_text("new base\n", encoding="utf-8")
    git(source, "add", "app.txt")
    git(source, "commit", "-m", "move base")
    moved = source_head(str(source), "main")
    with pytest.raises(WorkspaceError, match="Base branch moved"):
        merge_local(str(source), "candidate", clone.base_sha, "main")
    assert source_head(str(source), "main") == moved
    assert (source / "app.txt").read_text(encoding="utf-8") == "new base\n"


def test_bare_repository_delivery_uses_compare_and_swap(tmp_path: Path):
    seed = repository(tmp_path)
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "--bare", str(seed), str(bare)], check=True)
    clone = prepare_workspace(str(bare), tmp_path / "workspace", "main", "candidate")
    configure_identity(clone)
    apply_edits(clone, [{"path": "app.txt", "content": "bare\n"}], ["app.txt"])
    delivered = commit(clone, "bare delivery")
    publish_local(clone)
    assert merge_local(str(bare), "candidate", clone.base_sha, "main") == delivered
    assert source_head(str(bare), "main") == delivered


def test_open_pull_request_pushes_exact_branch_and_uses_body_file(tmp_path: Path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    item = GitWorkspace(
        root,
        "https://github.com/example/project.git",
        "main",
        "a" * 40,
        "candidate;literal",
    )
    calls: list[tuple[str, ...]] = []
    captured_body: list[str] = []

    monkeypatch.setattr(workspace_module, "_assert_workspace", lambda _workspace: root)

    def fake_git(*args, **_kwargs):
        calls.append(tuple(args))
        return subprocess.CompletedProcess(args, 0, "", "")

    def fake_gh(*args):
        calls.append(tuple(args))
        if args[:2] == ("pr", "create"):
            body_path = Path(args[args.index("--body-file") + 1])
            captured_body.append(body_path.read_text(encoding="utf-8"))
            return subprocess.CompletedProcess(
                args, 0, "https://github.com/example/project/pull/7\n", ""
            )
        return subprocess.CompletedProcess(
            args,
            0,
            json.dumps(
                {
                    "url": "https://github.com/example/project/pull/7",
                    "number": 7,
                    "headRefName": "candidate;literal",
                }
            ),
            "",
        )

    monkeypatch.setattr(workspace_module, "_git", fake_git)
    monkeypatch.setattr(workspace_module, "_run_gh", fake_gh)
    title = "title; $(not-a-command)"
    body = "body with `literal` text"
    result = open_pull_request(item, "example/project", "main", title, body)

    assert result == {
        "url": "https://github.com/example/project/pull/7",
        "number": 7,
        "head": "candidate;literal",
    }
    assert captured_body == [body]
    push = next(call for call in calls if call[0] == "push")
    assert push == (
        "push",
        item.source,
        "refs/heads/candidate;literal:refs/heads/candidate;literal",
    )
    create = next(call for call in calls if call[:2] == ("pr", "create"))
    assert create[create.index("--title") + 1] == title
    assert "--body-file" in create


def test_open_pull_request_rejects_a_different_remote_target(tmp_path: Path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    item = GitWorkspace(root, "https://github.com/example/one.git", "main", "a" * 40, "work")
    monkeypatch.setattr(workspace_module, "_assert_workspace", lambda _workspace: root)
    with pytest.raises(WorkspaceError, match="does not match"):
        open_pull_request(item, "example/two", "main", "title", "body")


def test_github_merge_asserts_head_and_never_uses_bypass_flags(monkeypatch):
    calls: list[tuple[str, ...]] = []

    def fake_gh(*args):
        calls.append(tuple(args))
        if len(calls) == 1:
            payload = {"url": "url", "number": 4, "headRefOid": "a" * 40, "state": "OPEN"}
        else:
            payload = {
                "url": "url",
                "number": 4,
                "headRefOid": "a" * 40,
                "state": "MERGED",
                "mergedAt": "now",
                "mergeCommit": {"oid": "b" * 40},
            }
        return subprocess.CompletedProcess(args, 0, json.dumps(payload), "")

    monkeypatch.setattr(workspace_module, "_run_gh", fake_gh)
    result = merge_pull_request("example/project", 4, "a" * 40)
    merge_args = calls[1]
    assert result["state"] == "MERGED"
    assert "--match-head-commit" in merge_args
    assert "--merge" in merge_args
    assert "--admin" not in merge_args

    calls.clear()

    def stale_gh(*args):
        calls.append(tuple(args))
        payload = {"headRefOid": "b" * 40}
        return subprocess.CompletedProcess(args, 0, json.dumps(payload), "")

    monkeypatch.setattr(workspace_module, "_run_gh", stale_gh)
    with pytest.raises(WorkspaceError, match="head changed"):
        merge_pull_request("example/project", 4, "a" * 40)
    assert len(calls) == 1


def test_wait_pull_request_polls_pending_checks_until_clean(monkeypatch):
    calls = 0

    def fake_gh(*args):
        nonlocal calls
        calls += 1
        pending = calls == 1
        payload = {
            "url": "url",
            "number": 4,
            "headRefOid": "a" * 40,
            "state": "OPEN",
            "mergeStateStatus": "BLOCKED" if pending else "CLEAN",
            "statusCheckRollup": [
                {
                    "__typename": "CheckRun",
                    "status": "IN_PROGRESS" if pending else "COMPLETED",
                    "conclusion": None if pending else "SUCCESS",
                }
            ],
        }
        return subprocess.CompletedProcess(args, 0, json.dumps(payload), "")

    monkeypatch.setattr(workspace_module, "_run_gh", fake_gh)
    monkeypatch.setattr(workspace_module, "PULL_REQUEST_POLL_SECONDS", 0.001)
    result = wait_pull_request("example/project", 4, "a" * 40, threading.Event(), 2)
    assert result["mergeStateStatus"] == "CLEAN"
    assert calls == 2


def test_wait_pull_request_rejects_failure_head_change_and_cancellation(monkeypatch):
    payload = {
        "headRefOid": "a" * 40,
        "state": "OPEN",
        "mergeStateStatus": "BLOCKED",
        "statusCheckRollup": [
            {"__typename": "StatusContext", "state": "FAILURE"},
        ],
    }
    monkeypatch.setattr(
        workspace_module,
        "_run_gh",
        lambda *args: subprocess.CompletedProcess(args, 0, json.dumps(payload), ""),
    )
    with pytest.raises(WorkspaceError, match="status check failed"):
        wait_pull_request("example/project", 4, "a" * 40, threading.Event(), 1)

    payload["headRefOid"] = "b" * 40
    with pytest.raises(WorkspaceError, match="head changed"):
        wait_pull_request("example/project", 4, "a" * 40, threading.Event(), 1)

    cancel = threading.Event()
    cancel.set()
    with pytest.raises(WorkspaceError, match="cancelled"):
        wait_pull_request("example/project", 4, "a" * 40, cancel, 1)


def test_wait_pull_request_allows_clean_no_check_pr_after_grace(monkeypatch):
    payload = {
        "headRefOid": "a" * 40,
        "state": "OPEN",
        "mergeStateStatus": "CLEAN",
        "statusCheckRollup": [],
    }
    monkeypatch.setattr(workspace_module, "NO_CHECK_GRACE_SECONDS", 0)
    monkeypatch.setattr(
        workspace_module,
        "_run_gh",
        lambda *args: subprocess.CompletedProcess(args, 0, json.dumps(payload), ""),
    )
    result = wait_pull_request("example/project", 4, "a" * 40, threading.Event(), 1)
    assert result["mergeStateStatus"] == "CLEAN"
