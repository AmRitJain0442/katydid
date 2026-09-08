"""Bounded Git workspace and delivery operations.

Local repositories passed to this module are trusted inputs.  Preparation still
uses a real clone so work in the resulting directory cannot change the source's
working tree or share mutable object files with it.
"""

import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import urlsplit

MAX_EDIT_BYTES = 200_000
MAX_GITHUB_BODY_BYTES = 1_000_000
GIT_TIMEOUT_SECONDS = 60
GH_TIMEOUT_SECONDS = 60
PULL_REQUEST_POLL_SECONDS = 2.0
NO_CHECK_GRACE_SECONDS = 10.0
_SHA = re.compile(r"[0-9a-fA-F]{40,64}\Z")
_GITHUB_PART = re.compile(r"[A-Za-z0-9_.-]+\Z")
_EDIT_MANIFEST = "katydid-edits-v1.json"


class WorkspaceError(RuntimeError):
    """A workspace or delivery operation could not be completed safely."""


@dataclass(frozen=True)
class GitWorkspace:
    path: Path
    source: str
    base_branch: str
    base_sha: str
    branch: str


def _run(
    argv: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    timeout: int = GIT_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            argv,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise WorkspaceError(f"Cannot run {argv[0]}: {exc}") from exc
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic"
        raise WorkspaceError(f"{argv[0]} exited with code {result.returncode}: {detail}")
    return result


def _git(
    *args: str, cwd: Path | None = None, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return _run(["git", *args], cwd=cwd, check=check)


def _validate_branch(branch: str) -> str:
    if not isinstance(branch, str) or not branch or "\x00" in branch:
        raise WorkspaceError("Git branch names must be non-empty strings")
    result = _git("check-ref-format", "--branch", branch, check=False)
    if result.returncode != 0:
        raise WorkspaceError(f"Invalid Git branch name: {branch!r}")
    return branch


def _github_repo(repo: str) -> str:
    if not isinstance(repo, str) or repo.count("/") != 1:
        raise WorkspaceError("GitHub repositories must use the owner/name form")
    owner, name = repo.split("/")
    if name.endswith(".git"):
        name = name[:-4]
    if (
        not owner
        or not name
        or not _GITHUB_PART.fullmatch(owner)
        or not _GITHUB_PART.fullmatch(name)
    ):
        raise WorkspaceError("GitHub repositories must use the owner/name form")
    return f"{owner}/{name}"


def _github_url(source: str) -> str | None:
    try:
        parsed = urlsplit(source)
    except ValueError:
        return None
    if parsed.scheme.lower() != "https" or parsed.hostname != "github.com":
        return None
    try:
        if parsed.username or parsed.password or parsed.port or parsed.query or parsed.fragment:
            return None
    except ValueError:
        return None
    if parsed.netloc.lower() != "github.com":
        return None
    parts = parsed.path.removeprefix("/").split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return None
    try:
        repo = _github_repo(f"{parts[0]}/{parts[1]}")
    except WorkspaceError:
        return None
    return f"https://github.com/{repo}.git"


def _source(source: str) -> tuple[str, Path | None]:
    if not isinstance(source, str) or not source or "\x00" in source:
        raise WorkspaceError("A repository source is required")
    github = _github_url(source)
    if github is not None:
        return github, None
    if "://" in source or source.startswith(("git@", "ssh:")):
        raise WorkspaceError("Remote sources must be HTTPS GitHub repository URLs")
    try:
        path = Path(source).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise WorkspaceError(f"Local repository source does not exist: {source}") from exc
    if not path.is_dir():
        raise WorkspaceError("A local repository source must be a directory")
    probe = _git("-C", str(path), "rev-parse", "--git-dir", check=False)
    if probe.returncode != 0:
        raise WorkspaceError(f"Local source is not a Git repository: {path}")
    return str(path), path


def _relative_path(value: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise WorkspaceError("File paths must be literal relative POSIX paths")
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if posix.is_absolute() or windows.is_absolute() or windows.drive:
        raise WorkspaceError(f"Absolute file path is not allowed: {value!r}")
    if value != posix.as_posix() or any(part in ("", ".", "..") for part in posix.parts):
        raise WorkspaceError(f"Normalized or traversing file path is not allowed: {value!r}")
    lowered = [part.casefold() for part in posix.parts]
    if ".git" in lowered:
        raise WorkspaceError("Paths inside .git are not allowed")
    name = lowered[-1]
    if name == ".env" or name.startswith(".env.") or name == ".envrc":
        raise WorkspaceError("Environment secret files are not allowed")
    if any(":" in part for part in posix.parts):
        raise WorkspaceError("Alternate data stream paths are not allowed")
    return value


def _is_link(path: Path) -> bool:
    info = path.lstat()
    attributes = getattr(info, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return path.is_symlink() or bool(attributes & reparse)


def _checked_path(root: Path, relative: str, *, may_be_missing: bool) -> Path:
    root = root.resolve(strict=True)
    if _is_link(root) or not root.is_dir():
        raise WorkspaceError("Workspace root must be a real directory")
    current = root
    parts = PurePosixPath(relative).parts
    for index, part in enumerate(parts):
        current = current / part
        try:
            if _is_link(current):
                raise WorkspaceError(f"Symlink or reparse-point path is not allowed: {relative}")
        except FileNotFoundError:
            if not may_be_missing or index != len(parts) - 1:
                raise WorkspaceError(f"File path does not exist: {relative}") from None
    try:
        resolved = current.resolve(strict=not may_be_missing)
    except FileNotFoundError:
        resolved = current.parent.resolve(strict=True) / current.name
    if not resolved.is_relative_to(root):
        raise WorkspaceError(f"File path escapes the workspace: {relative}")
    return current


def _allowed(allowed_paths: list[str]) -> tuple[str, ...]:
    if not isinstance(allowed_paths, list):
        raise WorkspaceError("allowed_paths must be a list")
    values = tuple(_relative_path(value) for value in allowed_paths)
    if len(set(values)) != len(values):
        raise WorkspaceError("allowed_paths contains duplicates")
    return values


def _assert_workspace(workspace: GitWorkspace) -> Path:
    try:
        if _is_link(workspace.path):
            raise WorkspaceError("Workspace root cannot be a symlink or reparse point")
        root = workspace.path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise WorkspaceError("Workspace root does not exist") from exc
    top = _git("rev-parse", "--show-toplevel", cwd=root).stdout.strip()
    if Path(top).resolve(strict=True) != root:
        raise WorkspaceError("Workspace path is not the root of its Git clone")
    branch = _git("symbolic-ref", "--quiet", "--short", "HEAD", cwd=root, check=False)
    if branch.returncode != 0 or branch.stdout.strip() != workspace.branch:
        raise WorkspaceError("Workspace is not checked out on its recorded branch")
    return root


def _manifest_path(root: Path) -> Path:
    git_dir = _git("rev-parse", "--absolute-git-dir", cwd=root).stdout.strip()
    return Path(git_dir) / _EDIT_MANIFEST


def _write_manifest(root: Path, paths: set[str]) -> None:
    destination = _manifest_path(root)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=destination.parent,
            prefix="katydid-edits-",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(sorted(paths), stream)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _read_manifest(root: Path) -> set[str]:
    path = _manifest_path(root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return set()
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkspaceError(f"Cannot read the edit manifest: {exc}") from exc
    if not isinstance(data, list) or not all(isinstance(item, str) for item in data):
        raise WorkspaceError("The edit manifest is invalid")
    return {_relative_path(item) for item in data}


def prepare_workspace(
    source: str,
    destination: Path,
    base_branch: str,
    branch: str,
    *,
    source_ref: str | None = None,
    source_sha: str | None = None,
) -> GitWorkspace:
    """Clone a repository independently and check out an exact, clean branch."""
    base_branch = _validate_branch(base_branch)
    branch = _validate_branch(branch)
    if (source_ref is None) != (source_sha is None):
        raise WorkspaceError("Source ref and SHA must be supplied together")
    if source_ref is not None:
        validate_source_ref(source_ref)
        if source_sha is None or not _SHA.fullmatch(source_sha):
            raise WorkspaceError("Source revision must be a full commit SHA")
    normalized_source, local_source = _source(source)
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise WorkspaceError("Workspace destination must not already exist")
    parent = destination.parent.resolve(strict=True)
    destination = parent / destination.name
    if local_source is not None and destination.is_relative_to(local_source):
        raise WorkspaceError("Workspace destination cannot be inside the source repository")

    clone_args = ["clone", "--no-checkout"]
    if local_source is not None:
        clone_args.extend(("--no-local", "--no-hardlinks"))
    clone_args.extend(("--", normalized_source, str(destination)))
    try:
        _git(*clone_args)
        _git("config", "--local", "user.name", "Katydid Automation", cwd=destination)
        _git(
            "config",
            "--local",
            "user.email",
            "katydid@users.noreply.github.com",
            cwd=destination,
        )
        _git("config", "--local", "commit.gpgSign", "false", cwd=destination)
        _git("config", "--local", "core.autocrlf", "false", cwd=destination)
        hooks = destination / ".git" / "katydid-hooks-disabled"
        hooks.mkdir()
        _git("config", "--local", "core.hooksPath", str(hooks), cwd=destination)
        remote_ref = f"refs/remotes/origin/{base_branch}^{{commit}}"
        base_sha = _git("rev-parse", "--verify", remote_ref, cwd=destination).stdout.strip()
        checkout_sha = base_sha
        if source_ref is not None:
            _git("fetch", "--no-tags", "origin", source_ref, cwd=destination)
            checkout_sha = _git(
                "rev-parse", "--verify", "FETCH_HEAD^{commit}", cwd=destination
            ).stdout.strip()
            if checkout_sha != source_sha:
                raise WorkspaceError("Source revision changed before checkout")
        _git("switch", "--force-create", branch, checkout_sha, cwd=destination)
        _git("reset", "--hard", checkout_sha, cwd=destination)
        _git("clean", "-ffd", cwd=destination)
        if _git("status", "--porcelain", cwd=destination).stdout:
            raise WorkspaceError("Prepared workspace is unexpectedly dirty")
        return GitWorkspace(destination, normalized_source, base_branch, base_sha, branch)
    except Exception:
        if destination.exists() and destination.resolve().is_relative_to(parent):
            shutil.rmtree(destination)
        raise


def snapshot_files(
    root: Path,
    allowed_paths: list[str],
    max_bytes: int = MAX_EDIT_BYTES,
) -> dict[str, str]:
    """Read an exact, bounded set of non-secret regular UTF-8 files."""
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0:
        raise WorkspaceError("max_bytes must be a non-negative integer")
    root = Path(root)
    try:
        if _is_link(root):
            raise WorkspaceError("Snapshot root cannot be a symlink or reparse point")
        root = root.resolve(strict=True)
    except FileNotFoundError as exc:
        raise WorkspaceError("Snapshot root does not exist") from exc
    paths = _allowed(allowed_paths)
    result: dict[str, str] = {}
    used = 0
    for relative in paths:
        path = _checked_path(root, relative, may_be_missing=True)
        if not path.exists():
            continue
        if not stat.S_ISREG(path.lstat().st_mode):
            raise WorkspaceError(f"Snapshot path is not a regular file: {relative}")
        remaining = max_bytes - used
        with path.open("rb") as stream:
            raw = stream.read(remaining + 1)
        if len(raw) > remaining:
            raise WorkspaceError(f"Snapshot exceeds the {max_bytes}-byte limit")
        try:
            result[relative] = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WorkspaceError(f"Snapshot file is not UTF-8: {relative}") from exc
        used += len(raw)
    return result


def apply_edits(
    workspace: GitWorkspace,
    edits: list[dict[str, str]],
    allowed_paths: list[str],
) -> list[str]:
    """Validate a complete edit batch, then atomically replace each named file."""
    root = _assert_workspace(workspace)
    allowed = set(_allowed(allowed_paths))
    if not isinstance(edits, list):
        raise WorkspaceError("edits must be a list")
    prepared: list[tuple[str, Path, bytes, int | None]] = []
    seen: set[str] = set()
    total = 0
    new_paths: list[str] = []
    for edit in edits:
        if not isinstance(edit, dict) or set(edit) != {"path", "content"}:
            raise WorkspaceError("Each edit must contain only string path and content fields")
        relative = _relative_path(edit["path"])
        content = edit["content"]
        if not isinstance(content, str):
            raise WorkspaceError("Edit content must be a string")
        if relative in seen:
            raise WorkspaceError(f"Duplicate edit path: {relative}")
        if relative not in allowed:
            raise WorkspaceError(f"Edit is outside the allowed file set: {relative}")
        seen.add(relative)
        path = _checked_path(root, relative, may_be_missing=True)
        mode: int | None = None
        if path.exists():
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise WorkspaceError(f"Edit target is not a regular file: {relative}")
            mode = stat.S_IMODE(info.st_mode)
        else:
            ignored = _git("check-ignore", "--quiet", "--", relative, cwd=root, check=False)
            if ignored.returncode == 0:
                raise WorkspaceError(f"Cannot create an ignored file: {relative}")
            new_paths.append(relative)
        raw = content.encode("utf-8")
        total += len(raw)
        if total > MAX_EDIT_BYTES:
            raise WorkspaceError(f"Edit batch exceeds the {MAX_EDIT_BYTES}-byte limit")
        prepared.append((relative, path, raw, mode))

    manifest = _read_manifest(root) | seen
    for _relative, path, raw, mode in prepared:
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=path.parent,
                prefix=".katydid-edit-",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            if mode is not None:
                os.chmod(temporary, mode)
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    if new_paths:
        _git("add", "--intent-to-add", "--", *new_paths, cwd=root)
    _write_manifest(root, manifest)
    return [relative for relative, _path, _raw, _mode in prepared]


def diff(workspace: GitWorkspace) -> str:
    """Return the complete tracked and intent-to-add patch against workspace HEAD."""
    root = _assert_workspace(workspace)
    return _git("diff", "--binary", "--no-ext-diff", "HEAD", "--", cwd=root).stdout


def commit(workspace: GitWorkspace, message: str) -> str:
    """Commit only paths previously passed to apply_edits."""
    root = _assert_workspace(workspace)
    if not isinstance(message, str) or not message.strip() or "\x00" in message:
        raise WorkspaceError("Commit message must be a non-empty string")
    paths = sorted(_read_manifest(root))
    if not paths:
        raise WorkspaceError("There are no Katydid edits to commit")
    for relative in paths:
        _checked_path(root, relative, may_be_missing=False)
    status = _git(
        "status", "--porcelain=v1", "--untracked-files=all", "--", *paths, cwd=root
    ).stdout
    if not status:
        raise WorkspaceError("Katydid edit paths contain no changes to commit")
    _git("commit", "--only", "-m", message, "--", *paths, cwd=root)
    sha = _git("rev-parse", "HEAD", cwd=root).stdout.strip()
    _manifest_path(root).unlink(missing_ok=True)
    return sha


def source_head(source: str, branch: str) -> str:
    """Resolve one exact branch head without checking out or mutating the source."""
    branch = _validate_branch(branch)
    normalized_source, _local = _source(source)
    ref = f"refs/heads/{branch}"
    result = _git("ls-remote", "--exit-code", "--heads", normalized_source, ref)
    rows = [line.split("\t", 1) for line in result.stdout.splitlines() if line]
    matches = [sha for sha, found_ref in rows if found_ref == ref and _SHA.fullmatch(sha)]
    if len(matches) != 1:
        raise WorkspaceError(f"Source branch could not be resolved exactly: {branch}")
    return matches[0].lower()


def validate_source_ref(ref: str) -> None:
    """Accept only full branch, tag, and GitHub pull-request head references."""
    if not isinstance(ref, str) or not (
        ref.startswith(("refs/heads/", "refs/tags/"))
        or re.fullmatch(r"refs/pull/[1-9][0-9]*/head", ref)
    ):
        raise WorkspaceError("Expected a full branch, tag, or pull-request head ref")
    if _git("check-ref-format", ref, check=False).returncode != 0:
        raise WorkspaceError("Invalid source ref")


def source_revision(source: str, ref: str) -> str:
    """Resolve one approved ref, peeling annotated tags to their immutable commit."""
    validate_source_ref(ref)
    normalized, _local = _source(source)
    result = _git("ls-remote", "--exit-code", normalized, ref, f"{ref}^{{}}")
    rows = [line.split("\t", 1) for line in result.stdout.splitlines() if line]
    found = {name: sha for sha, name in rows if _SHA.fullmatch(sha)}
    sha = found.get(f"{ref}^{{}}", found.get(ref))
    if sha is None:
        raise WorkspaceError("Source ref could not be resolved exactly")
    return sha.lower()


def tree_sha(source: str | Path, commit_sha: str) -> str:
    """Resolve a commit's tree in a local repository for delivery comparison."""
    normalized_source, local = _source(str(source))
    if local is None:
        raise WorkspaceError("tree_sha requires a local repository source")
    if not isinstance(commit_sha, str) or not _SHA.fullmatch(commit_sha):
        raise WorkspaceError("commit_sha must be a full commit SHA")
    value = _git("rev-parse", "--verify", f"{commit_sha}^{{tree}}", cwd=local).stdout.strip()
    if not _SHA.fullmatch(value):
        raise WorkspaceError(f"Cannot resolve tree for commit in {normalized_source}")
    return value.lower()


def publish_local(workspace: GitWorkspace) -> str:
    """Push the workspace branch to its local source using normal fast-forward rules."""
    root = _assert_workspace(workspace)
    normalized_source, local = _source(workspace.source)
    if local is None:
        raise WorkspaceError("publish_local requires a local repository source")
    ref = f"refs/heads/{workspace.branch}"
    _git("push", normalized_source, f"{ref}:{ref}", cwd=root)
    return source_head(normalized_source, workspace.branch)


def _checked_out_branches(source: Path) -> dict[str, Path]:
    result = _git("worktree", "list", "--porcelain", cwd=source)
    found: dict[str, Path] = {}
    worktree: Path | None = None
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            worktree = Path(line.removeprefix("worktree ")).resolve()
        elif line.startswith("branch refs/heads/") and worktree is not None:
            found[line.removeprefix("branch refs/heads/")] = worktree
    return found


def merge_local(source: str, branch: str, expected_base_sha: str, base_branch: str) -> str:
    """Fast-forward a local base branch only when its head matches the expected SHA."""
    branch = _validate_branch(branch)
    base_branch = _validate_branch(base_branch)
    if not isinstance(expected_base_sha, str) or not _SHA.fullmatch(expected_base_sha):
        raise WorkspaceError("expected_base_sha must be a full commit SHA")
    normalized_source, local = _source(source)
    if local is None:
        raise WorkspaceError("merge_local requires a local repository source")
    current = source_head(normalized_source, base_branch)
    expected = expected_base_sha.lower()
    if current != expected:
        raise WorkspaceError(f"Base branch moved: expected {expected}, found {current}")
    target_ref = f"refs/heads/{branch}^{{commit}}"
    target = _git("rev-parse", "--verify", target_ref, cwd=local).stdout.strip().lower()
    ancestor = _git("merge-base", "--is-ancestor", expected, target, cwd=local, check=False)
    if ancestor.returncode != 0:
        raise WorkspaceError("Delivery branch is not a fast-forward from the expected base")

    checked_out = _checked_out_branches(local)
    checkout = checked_out.get(base_branch)
    if checkout is not None:
        if checkout != local.resolve():
            raise WorkspaceError("Base branch is checked out in another worktree")
        dirty = _git("status", "--porcelain", cwd=local).stdout
        if dirty:
            raise WorkspaceError(
                "Source working tree must be clean before merging its checked-out branch"
            )
        # Git's merge owns the index and worktree update; the exact-head check above is
        # repeated by verifying the result immediately after its ff-only operation.
        _git("merge", "--ff-only", "--no-edit", target, cwd=local)
        merged = source_head(normalized_source, base_branch)
        if merged != target:
            raise WorkspaceError("Base branch changed concurrently during merge")
        return merged

    result = _git(
        "update-ref",
        f"refs/heads/{base_branch}",
        target,
        expected,
        cwd=local,
        check=False,
    )
    if result.returncode != 0:
        raise WorkspaceError("Base branch changed concurrently; compare-and-swap failed")
    return target


def _run_gh(*args: str) -> subprocess.CompletedProcess[str]:
    return _run(["gh", *args], timeout=GH_TIMEOUT_SECONDS)


def _gh_json(*args: str) -> dict[str, Any]:
    raw = _run_gh(*args).stdout
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WorkspaceError("GitHub CLI returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise WorkspaceError("GitHub CLI returned an unexpected response")
    return value


def open_pull_request(
    workspace: GitWorkspace,
    repo: str,
    base: str,
    title: str,
    body: str,
) -> dict[str, Any]:
    """Open a GitHub pull request using argument-array and body-file boundaries."""
    root = _assert_workspace(workspace)
    repo = _github_repo(repo)
    expected_source = f"https://github.com/{repo}.git"
    if workspace.source.casefold() != expected_source.casefold():
        raise WorkspaceError("Pull request repository does not match the workspace source")
    base = _validate_branch(base)
    if not isinstance(title, str) or not title.strip() or "\x00" in title:
        raise WorkspaceError("Pull request title must be a non-empty string")
    if not isinstance(body, str) or "\x00" in body:
        raise WorkspaceError("Pull request body must be a string without NUL bytes")
    if len(body.encode("utf-8")) > MAX_GITHUB_BODY_BYTES:
        raise WorkspaceError("Pull request body exceeds the byte limit")
    branch_ref = f"refs/heads/{workspace.branch}"
    _git("push", workspace.source, f"{branch_ref}:{branch_ref}", cwd=root)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix="katydid-pr-", suffix=".md", delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(body)
        created = _run_gh(
            "pr",
            "create",
            "--repo",
            repo,
            "--base",
            base,
            "--head",
            workspace.branch,
            "--title",
            title,
            "--body-file",
            str(temporary),
        ).stdout.strip()
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    if not created:
        raise WorkspaceError("GitHub CLI did not return the created pull request URL")
    value = _gh_json("pr", "view", created, "--repo", repo, "--json", "url,number,headRefName")
    url = value.get("url")
    number = value.get("number")
    head = value.get("headRefName")
    if (
        not isinstance(url, str)
        or isinstance(number, bool)
        or not isinstance(number, int)
        or number <= 0
        or head != workspace.branch
    ):
        raise WorkspaceError("GitHub did not confirm the created pull request identity")
    return {"url": url, "number": number, "head": head}


def _check_state(check: object) -> str:
    if not isinstance(check, dict):
        return "pending"
    kind = check.get("__typename")
    if kind == "CheckRun" or "conclusion" in check:
        status = str(check.get("status", "")).upper()
        conclusion = str(check.get("conclusion") or "").upper()
        if status != "COMPLETED" or not conclusion:
            return "pending"
        if conclusion in {"SUCCESS", "NEUTRAL", "SKIPPED"}:
            return "passed"
        return "failed"
    state = str(check.get("state", "")).upper()
    if state == "SUCCESS":
        return "passed"
    if state in {"ERROR", "FAILURE"}:
        return "failed"
    return "pending"


def wait_pull_request(
    repo: str,
    number: int,
    expected_head: str,
    cancel: threading.Event,
    timeout_seconds: int = 900,
) -> dict[str, Any]:
    """Wait boundedly for one exact PR head to have passing checks and be clean."""
    repo = _github_repo(repo)
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        raise WorkspaceError("Pull request number must be a positive integer")
    if not isinstance(expected_head, str) or not _SHA.fullmatch(expected_head):
        raise WorkspaceError("expected_head must be a full commit SHA")
    if not isinstance(cancel, threading.Event):
        raise WorkspaceError("cancel must be a threading.Event")
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int)
        or timeout_seconds <= 0
    ):
        raise WorkspaceError("timeout_seconds must be a positive integer")
    expected_head = expected_head.lower()
    started = time.monotonic()
    deadline = started + timeout_seconds
    while True:
        if cancel.is_set():
            raise WorkspaceError("Pull request wait was cancelled")
        value = _gh_json(
            "pr",
            "view",
            str(number),
            "--repo",
            repo,
            "--json",
            "url,number,headRefOid,state,statusCheckRollup,mergeStateStatus",
        )
        if str(value.get("headRefOid", "")).lower() != expected_head:
            raise WorkspaceError("Pull request head changed while waiting")
        if str(value.get("state", "")).upper() != "OPEN":
            raise WorkspaceError("Pull request is no longer open")
        checks = value.get("statusCheckRollup")
        if not isinstance(checks, list):
            raise WorkspaceError("GitHub returned invalid pull request check data")
        states = [_check_state(check) for check in checks]
        if "failed" in states:
            raise WorkspaceError("A pull request status check failed")
        merge_state = str(value.get("mergeStateStatus", "")).upper()
        elapsed = time.monotonic() - started
        checks_ready = bool(checks) and "pending" not in states
        no_checks_ready = not checks and elapsed >= NO_CHECK_GRACE_SECONDS
        if (checks_ready or no_checks_ready) and merge_state == "CLEAN":
            return value
        if merge_state == "DIRTY":
            raise WorkspaceError("Pull request has merge conflicts")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise WorkspaceError("Timed out waiting for pull request checks")
        cancel.wait(min(PULL_REQUEST_POLL_SECONDS, remaining))


def merge_pull_request(repo: str, number: int, expected_head: str) -> dict[str, Any]:
    """Merge a PR without bypass flags after asserting its current head SHA."""
    repo = _github_repo(repo)
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        raise WorkspaceError("Pull request number must be a positive integer")
    if not isinstance(expected_head, str) or not _SHA.fullmatch(expected_head):
        raise WorkspaceError("expected_head must be a full commit SHA")
    expected_head = expected_head.lower()
    current = _gh_json(
        "pr", "view", str(number), "--repo", repo, "--json", "url,number,headRefOid,state"
    )
    if str(current.get("headRefOid", "")).lower() != expected_head:
        raise WorkspaceError("Pull request head changed; refusing to merge")
    _run_gh(
        "pr",
        "merge",
        str(number),
        "--repo",
        repo,
        "--merge",
        "--match-head-commit",
        expected_head,
    )
    merged = _gh_json(
        "pr",
        "view",
        str(number),
        "--repo",
        repo,
        "--json",
        "url,number,headRefOid,state,mergedAt,mergeCommit",
    )
    merge_commit = merged.get("mergeCommit")
    merge_sha = merge_commit.get("oid") if isinstance(merge_commit, dict) else None
    if str(merged.get("state", "")).upper() != "MERGED" or not isinstance(merge_sha, str):
        raise WorkspaceError("GitHub did not confirm a completed pull request merge")
    if not _SHA.fullmatch(merge_sha):
        raise WorkspaceError("GitHub returned an invalid merge commit SHA")
    return merged
