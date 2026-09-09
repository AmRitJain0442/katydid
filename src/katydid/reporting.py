"""Durable, opt-in GitHub PR status comments, independent of execution gates."""

import hashlib
import html
import json
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

from katydid.fleet import RepositoryConfig
from katydid.live import workflow_snapshot

SHA = re.compile(r"^[a-f0-9]{40}(?:[a-f0-9]{24})?$")
REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
TASK = re.compile(r"^[a-f0-9]{32}$")


class ReportingError(RuntimeError):
    pass


def public_text(value: Any, limit: int = 1200) -> str:
    """Escape report prose and remove common credentials/host-local paths."""
    text = str(value or "")[:12000]
    text = re.sub(
        r"-----BEGIN[^-]*PRIVATE KEY-----.*?(?:-----END[^-]*PRIVATE KEY-----|$)",
        "[redacted key]",
        text,
        flags=re.S,
    )
    text = re.sub(
        r"(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|AIza[A-Za-z0-9_-]+)",
        "[redacted token]",
        text,
    )
    text = re.sub(
        r"(?i)\b(?:bearer\s+\S+|(?:api[_-]?key|access[_-]?token|password|secret)\s*[:=]\s*[\"']?[^\s,;\"']+)",
        "[redacted credential]",
        text,
    )
    text = re.sub(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[redacted token]", text)
    text = re.sub(
        r"[A-Za-z]:[\\/][^\s`\"<>]+|/(?:Users|home|tmp|var|mnt)/[^\s`\"<>]+", "[host path]", text
    )
    text = re.sub(r"https?://[^\s<>]+", "[link omitted]", text)
    text = " ".join(text.split())[:limit]
    return (
        html.escape(text)
        .replace("@", "＠")
        .replace("|", "&#124;")
        .replace("`", "&#96;")
        .replace("[", "&#91;")
        .replace("]", "&#93;")
    )


def target_for(
    repo: RepositoryConfig, task: dict[str, Any], events: list[dict[str, Any]]
) -> dict[str, Any] | None:
    if not repo.github_comments or not repo.source.startswith("https://github.com/"):
        return None
    repository = repo.source.removeprefix("https://github.com/").removesuffix(".git")
    if not REPO.fullmatch(repository) or not TASK.fullmatch(str(task.get("id", ""))):
        return None
    payload = task.get("payload") or {}
    ref = str(payload.get("source_ref") or "")
    match = re.fullmatch(r"refs/pull/([1-9][0-9]*)/head", ref)
    number, head = None, None
    if match:
        metadata = payload.get("event") or {}
        if (
            metadata.get("provider") != "github"
            or metadata.get("type") != "pull_request"
            or str(metadata.get("repository", "")).casefold() != repository.casefold()
        ):
            return None
        number = int(match[1])
        if type(metadata.get("number")) is not int or metadata.get("number") != number:
            return None
        head = payload.get("source_sha")
    else:
        # The controller supplies this association after GitHub confirms PR creation.
        candidates = [
            event.get("details", {}) for event in events if event.get("kind") == "transition"
        ]
        candidates.append(task.get("result") or {})
        for detail in candidates:
            pull = detail.get("pull_request") or {}
            candidate = pull.get("number")
            if (
                isinstance(candidate, int)
                and not isinstance(candidate, bool)
                and candidate > 0
                and pull.get("url") == f"https://github.com/{repository}/pull/{candidate}"
            ):
                number, head = candidate, detail.get("candidate_sha")
    if number is None or not isinstance(head, str) or not SHA.fullmatch(head):
        return None
    return {
        "repository": repository,
        "number": number,
        "head": head,
        "task_id": task["id"],
        "base": repo.base_branch,
    }


def comment_body(task: dict[str, Any], target: dict[str, Any], workflow: dict[str, Any]) -> str:
    marker = f"<!-- vultron:investigation:{target['task_id']} -->"
    repository, number, head = target["repository"], target["number"], target["head"]
    result = task.get("result") or {}
    rows = [step for run in workflow.get("runs", []) for step in run.get("steps", [])]
    running = [step for step in rows if step.get("status") == "running"]
    lines = [
        marker,
        "## Vultron investigation",
        "",
        f"**Status:** {public_text(task['state'], 50)}",
        f"**Investigation revision:** [{head[:12]}](https://github.com/{repository}/commit/{head})",
        f"**Investigation:** `{target['task_id']}` · execution {int(task.get('epoch', 0))}",
        "",
    ]
    if running:
        step = running[0]
        lines += [
            f"**Executing:** {public_text(step.get('tool') or step.get('name'), 80)} — "
            f"{public_text(step.get('name'), 80)}",
            "",
        ]
    if rows:
        lines += ["| Tool | Check | Status |", "| --- | --- | --- |"]
        for step in rows[-60:]:
            lines.append(
                f"| {public_text(step.get('tool') or step.get('name'), 80)} | "
                f"{public_text(step.get('name'), 80)} | {public_text(step.get('status'), 40)} |"
            )
        if len(rows) > 60:
            lines.append("Earlier check attempts are retained in Vultron.")
        lines.append("")
    for field, label in (("diagnosis", "AI diagnosis"), ("review", "AI review")):
        value = result.get(field)
        if isinstance(value, dict) and value.get("summary"):
            lines += [f"**{label}:** {public_text(value['summary'])}", ""]
    if result.get("outcome"):
        lines += [f"**Outcome:** {public_text(result['outcome'], 100)}", ""]
    if result.get("error"):
        lines += [f"**Stopped:** {public_text(result['error'], 600)}", ""]
    lines += [
        f"[GitHub checks and evidence](https://github.com/{repository}/pull/{number}/checks)",
        "",
        "Full tool logs remain on the Vultron host. This comment reports the revision above "
        "and does not approve or merge the PR.",
    ]
    return "\n".join(lines)


class GitHubComments:
    def __init__(self, stop: threading.Event | None = None) -> None:
        self.stop = stop or threading.Event()
        self.actor: int | None = None

    def request(self, path: str, method: str = "GET", body: dict[str, Any] | None = None) -> Any:
        if self.stop.is_set():
            raise ReportingError("Comment reporter is stopping")
        argv = [
            "gh",
            "api",
            "--hostname",
            "github.com",
            "--method",
            method,
            "-H",
            "Accept: application/vnd.github+json",
            path,
        ]
        with tempfile.TemporaryDirectory(prefix="vultron-comment-") as temporary:
            if body is not None:
                payload = Path(temporary) / "body.json"
                payload.write_text(json.dumps(body), encoding="utf-8")
                argv += ["--input", str(payload)]
            try:
                process = subprocess.run(
                    argv,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=20,
                    shell=False,
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise ReportingError("GitHub connection unavailable or timed out") from exc
        if process.returncode:
            status = re.search(r"HTTP (\d{3})", process.stderr)
            raise ReportingError(
                f"GitHub rejected the request (HTTP {status[1]})"
                if status
                else "GitHub request failed; check connectivity and authentication"
            )
        if len(process.stdout.encode("utf-8")) > 8 * 1024 * 1024:
            raise ReportingError("GitHub response exceeded the reporting budget")
        try:
            return json.loads(process.stdout)
        except ValueError as exc:
            raise ReportingError("GitHub returned invalid JSON") from exc

    def upsert(
        self, target: dict[str, Any], body: str, before_write: Callable[[], None] | None = None
    ) -> dict[str, Any]:
        repo, number, task_id, head = (
            target["repository"],
            target["number"],
            target["task_id"],
            target["head"],
        )
        if (
            not REPO.fullmatch(repo)
            or type(number) is not int
            or number <= 0
            or not TASK.fullmatch(task_id)
            or not SHA.fullmatch(head)
        ):
            raise ReportingError("Invalid comment target")
        marker = f"<!-- vultron:investigation:{task_id} -->"
        if not body.startswith(marker + "\n") or len(body.encode("utf-8")) > 60000:
            raise ReportingError("Invalid or oversized comment body")
        pull = self.request(f"repos/{repo}/pulls/{number}")
        if (
            not isinstance(pull, dict)
            or pull.get("number") != number
            or pull.get("base", {}).get("repo", {}).get("full_name", "").casefold()
            != repo.casefold()
            or pull.get("base", {}).get("ref") != target["base"]
        ):
            raise ReportingError("GitHub did not confirm the registered PR target")
        superseded = pull.get("head", {}).get("sha") != head
        actor = self.request("user")
        if not isinstance(actor, dict) or type(actor.get("id")) is not int or actor["id"] <= 0:
            raise ReportingError("GitHub did not confirm the commenting account")
        self.actor = actor["id"]
        matches = []
        for page in range(1, 11):
            comments = self.request(
                f"repos/{repo}/issues/{number}/comments?per_page=100&page={page}"
            )
            if not isinstance(comments, list):
                raise ReportingError("GitHub returned invalid comment data")
            matches += [
                item
                for item in comments
                if isinstance(item, dict)
                and str(item.get("body", "")).startswith(marker + "\n")
                and item.get("user", {}).get("id") == self.actor
            ]
            if len(comments) < 100:
                break
        else:
            raise ReportingError(
                "Comment discovery exceeded 1000 comments; refusing duplicate creation"
            )
        if len(matches) > 1:
            raise ReportingError(
                "Multiple owned investigation comments found; refusing an ambiguous update"
            )
        latest = self.request(f"repos/{repo}/pulls/{number}")
        if (
            not isinstance(latest, dict)
            or latest.get("number") != number
            or latest.get("base", {}).get("repo", {}).get("full_name", "").casefold()
            != repo.casefold()
            or latest.get("base", {}).get("ref") != target["base"]
        ):
            raise ReportingError("PR target changed during comment reconciliation")
        superseded = superseded or latest.get("head", {}).get("sha") != head
        if before_write:
            before_write()
        if superseded:
            if not matches:
                return {"state": "superseded", "comment_id": None, "url": None}
            body = (
                marker + "\n## Vultron investigation\n\n**Superseded:** "
                f"this investigation tested `{head}`. "
                "The PR head has changed; its earlier results do not describe the current revision."
                f"\n\nInvestigation: `{task_id}`."
            )
        if matches:
            comment_id = matches[0].get("id")
            if type(comment_id) is not int or comment_id <= 0:
                raise ReportingError("Invalid owned comment identity")
            response = (
                matches[0]
                if matches[0].get("body") == body
                else self.request(
                    f"repos/{repo}/issues/comments/{comment_id}", "PATCH", {"body": body}
                )
            )
        else:
            response = self.request(
                f"repos/{repo}/issues/{number}/comments", "POST", {"body": body}
            )
        if (
            not isinstance(response, dict)
            or type(response.get("id")) is not int
            or response.get("id", 0) <= 0
            or (matches and response.get("id") != comment_id)
            or response.get("user", {}).get("id") != self.actor
            or response.get("body") != body
        ):
            raise ReportingError("GitHub did not confirm the published comment")
        comment_id = response["id"]
        url = f"https://github.com/{repo}/pull/{number}#issuecomment-{comment_id}"
        return {
            "state": "superseded" if superseded else "posted",
            "comment_id": comment_id,
            "url": url,
        }


class CommentReporter:
    """A durable outbox with coalescing, retry backoff, and one in-flight writer per task."""

    def __init__(
        self, controller: Any, stop: threading.Event, client: GitHubComments | None = None
    ) -> None:
        self.controller = controller
        self.stop = stop
        self.client = client or GitHubComments(stop)
        self.path = controller.directory / "github-comments.db"
        with closing(self.connect()) as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS reports (
                    task_id TEXT PRIMARY KEY, target TEXT NOT NULL, body TEXT NOT NULL,
                    body_hash TEXT NOT NULL, sent_hash TEXT, state TEXT NOT NULL DEFAULT 'pending',
                    comment_id INTEGER, url TEXT, error TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt REAL NOT NULL DEFAULT 0, locked_until REAL NOT NULL DEFAULT 0
                )"""
            )

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        return connection

    def collect(self) -> None:
        self.controller._unchanged_policy()
        for task in self.controller.store.list_tasks():
            if (task.get("payload") or {}).get("config_sha256") != self.controller.digest:
                continue
            repo = self.controller.config.repository(task["repository"])
            if not repo.github_comments:
                continue
            target = target_for(repo, task, self.controller.store.events(task["id"]))
            if target is None:
                continue
            workflow = workflow_snapshot(self.controller.directory, task, [])
            body = comment_body(task, target, workflow)
            digest = hashlib.sha256(body.encode()).hexdigest()
            encoded_target = json.dumps(target, sort_keys=True)
            with closing(self.connect()) as connection:
                connection.execute(
                    """INSERT INTO reports(task_id,target,body,body_hash) VALUES(?,?,?,?)
                    ON CONFLICT(task_id) DO UPDATE SET body=excluded.body,
                    body_hash=excluded.body_hash,
                    state=CASE WHEN reports.body_hash!=excluded.body_hash
                        AND reports.state!='superseded' THEN 'pending' ELSE reports.state END
                    WHERE reports.target=excluded.target""",
                    (task["id"], encoded_target, body, digest),
                )

    def tick(self) -> None:
        self.collect()
        if self.stop.is_set():
            return
        now = time.time()
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT * FROM reports WHERE body_hash!=COALESCE(sent_hash,'')
                AND state NOT IN ('superseded','disabled') AND next_attempt<=? AND locked_until<=?
                ORDER BY next_attempt,task_id LIMIT 1""",
                (now, now),
            ).fetchone()
            if row is None:
                connection.commit()
                return
            connection.execute(
                "UPDATE reports SET locked_until=? WHERE task_id=?", (now + 300, row["task_id"])
            )
            connection.commit()
        try:
            self.controller._unchanged_policy()
            task = self.controller.store.get_task(row["task_id"])
            repo = self.controller.config.repository(task["repository"])
            target = target_for(repo, task, self.controller.store.events(task["id"]))
            if (
                target is None
                or not repo.github_comments
                or json.dumps(target, sort_keys=True) != row["target"]
                or task["payload"].get("config_sha256") != self.controller.digest
            ):
                with closing(self.connect()) as connection:
                    connection.execute(
                        "UPDATE reports SET state='disabled',locked_until=0 WHERE task_id=?",
                        (row["task_id"],),
                    )
                return
            result = self.client.upsert(target, row["body"], self.controller._unchanged_policy)
            with closing(self.connect()) as connection:
                connection.execute(
                    """UPDATE reports SET sent_hash=?,
                    state=CASE WHEN body_hash=? THEN ? ELSE 'pending' END,
                    comment_id=?,url=?,error=NULL,attempts=0,next_attempt=?,locked_until=0
                    WHERE task_id=?""",
                    (
                        row["body_hash"],
                        row["body_hash"],
                        result["state"],
                        result["comment_id"],
                        result["url"],
                        time.time() + 15,
                        row["task_id"],
                    ),
                )
        except Exception as exc:
            error = (
                str(exc)
                if isinstance(exc, ReportingError)
                else "Comment reporting failed; retry pending"
            )
            with closing(self.connect()) as connection:
                connection.execute(
                    """UPDATE reports SET state='retrying',error=?,attempts=attempts+1,
                    next_attempt=?,locked_until=0 WHERE task_id=?""",
                    (
                        error[:300],
                        time.time() + min(300, 15 * 2 ** min(row["attempts"], 5)),
                        row["task_id"],
                    ),
                )

    def status(self, task: dict[str, Any]) -> dict[str, Any]:
        repo = self.controller.config.repository(task["repository"])
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT state,url,error,attempts FROM reports WHERE task_id=?", (task["id"],)
            ).fetchone()
        return {
            "enabled": repo.github_comments,
            **(dict(row) if row else {"state": "unassociated", "url": None, "error": None}),
        }

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception:
                pass  # Network/reporting failures cannot terminate the test worker.
            self.stop.wait(15)
