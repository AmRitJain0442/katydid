"""Authenticated GitHub event normalization and durable task ingestion."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Callable
from typing import Any, Protocol

from katydid.store import EventConflict, EventNotFound
from katydid.workspace import WorkspaceError, source_revision

MAX_BODY_BYTES = 1024 * 1024
MAX_RECONCILE_ATTEMPTS = 3
_DELIVERY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,127}$")
_EVENT = re.compile(r"^[a-z][a-z_]{0,63}$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_PULL_ACTIONS = frozenset(
    {"opened", "reopened", "synchronize", "ready_for_review", "edited", "closed"}
)


class EventError(RuntimeError):
    """Base error for rejected provider events."""


class InvalidEvent(EventError):
    """The authenticated delivery does not have the required GitHub shape."""


class UnknownRepository(EventError):
    """The delivery is not for an event-enabled registered repository."""


class EventRace(EventError):
    """Provider state changed repeatedly while the event was reconciled."""


class PullRequestReader(Protocol):
    def __call__(self, repository: str, number: int) -> dict[str, Any]: ...


class RevisionReader(Protocol):
    def __call__(self, source: str, ref: str) -> str: ...


def _json_object(body: bytes) -> dict[str, Any]:
    if not isinstance(body, bytes) or not body or len(body) > MAX_BODY_BYTES:
        raise InvalidEvent("Webhook body must contain between 1 byte and 1 MiB")
    try:
        source = body.decode("utf-8", errors="strict")

        def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in values:
                if key in result:
                    raise InvalidEvent(f"Duplicate JSON member: {key}")
                result[key] = value
            return result

        def constant(_value: str) -> Any:
            raise InvalidEvent("Non-finite JSON numbers are not supported")

        value: object = json.loads(source, object_pairs_hook=pairs, parse_constant=constant)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise InvalidEvent("Webhook body must be one strict UTF-8 JSON object") from exc
    if not isinstance(value, dict):
        raise InvalidEvent("Webhook body must be a JSON object")
    return value


def _object(parent: dict[str, Any], key: str) -> dict[str, Any]:
    value = parent.get(key)
    if not isinstance(value, dict):
        raise InvalidEvent(f"GitHub payload needs object member {key!r}")
    return value


def _string(parent: dict[str, Any], key: str, maximum: int = 512) -> str:
    value = parent.get(key)
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or "\x00" in value
    ):
        raise InvalidEvent(f"GitHub payload needs normalized string member {key!r}")
    return value


def _integer(parent: dict[str, Any], key: str) -> int:
    value = parent.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise InvalidEvent(f"GitHub payload needs positive integer member {key!r}")
    return value


def _sha(parent: dict[str, Any], key: str) -> str:
    value = _string(parent, key, 40)
    if _SHA.fullmatch(value) is None:
        raise InvalidEvent(f"GitHub payload member {key!r} must be a full commit SHA")
    return value


def _group(kind: str, value: str) -> str:
    candidate = f"github:{kind}:{value}"
    if len(candidate) <= 256:
        return candidate
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return f"github:{kind}:sha256:{digest}"


def _gh_pull_request(repository: str, number: int) -> dict[str, Any]:
    argv = ["gh", "api", "--method", "GET", f"repos/{repository}/pulls/{number}"]
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise EventError(f"Cannot query current GitHub pull request state: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic"
        raise EventError(f"GitHub pull request query failed: {detail}")
    try:
        value: object = json.loads(result.stdout)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise EventError("GitHub returned invalid pull request JSON") from exc
    if not isinstance(value, dict):
        raise EventError("GitHub returned invalid pull request data")
    return value


class GitHubIngress:
    """Turn exact current GitHub state into deduplicated controller tasks."""

    def __init__(
        self,
        controller: Any,
        pull_request: PullRequestReader | None = None,
        revision: RevisionReader | None = None,
    ) -> None:
        self.controller = controller
        self.pull_request = pull_request or _gh_pull_request
        self.revision = revision or source_revision

    def _repository(self, payload: dict[str, Any]) -> tuple[Any, str]:
        github_repository = _string(_object(payload, "repository"), "full_name", 200)
        matches = [
            repository
            for repository in self.controller.config.repositories
            if repository.events is not None
            and repository.events.github_repository.casefold() == github_repository.casefold()
        ]
        if len(matches) != 1:
            raise UnknownRepository("GitHub repository is not registered for event ingestion")
        return matches[0], github_repository

    def _duplicate(
        self, delivery_id: str, body_sha256: str, replay_key: str, repository: Any
    ) -> dict[str, Any] | None:
        try:
            receipt = self.controller.store.get_event("github", delivery_id)
        except EventNotFound:
            return None
        if (
            receipt["body_sha256"] != body_sha256
            or receipt["repository"] != repository.id
            or (receipt.get("replay_key") is not None and receipt.get("replay_key") != replay_key)
        ):
            raise EventConflict("GitHub delivery ID was reused with different content or scope")
        return self._receipt_status({**receipt, "duplicate": True})

    @staticmethod
    def _receipt_status(receipt: dict[str, Any]) -> dict[str, Any]:
        if receipt["task_id"] is not None:
            status = "enqueued"
        elif receipt["group"] is None or str(receipt["group"]).startswith("github:release:"):
            status = "ignored"
        else:
            status = "cancelled"
        return {**receipt, "status": status}

    def _replay(
        self,
        delivery_id: str,
        body_sha256: str,
        replay_key: str,
        repository: Any,
    ) -> dict[str, Any] | None:
        try:
            self.controller.store.get_replay("github", repository.id, replay_key)
        except EventNotFound:
            return None
        receipt = self.controller.store.ingest_event(
            "github",
            delivery_id,
            body_sha256,
            repository.id,
            None,
            replay_key=replay_key,
        )
        return self._receipt_status(receipt)

    def _ignored(
        self,
        delivery_id: str,
        body_sha256: str,
        replay_key: str,
        repository: Any,
        reason: str,
    ) -> dict[str, Any]:
        receipt = self.controller.store.ingest_event(
            "github",
            delivery_id,
            body_sha256,
            repository.id,
            None,
            replay_key=replay_key,
        )
        return {**receipt, "status": "ignored", "reason": reason}

    @staticmethod
    def _event_metadata(
        event: str,
        delivery_id: str,
        github_repository: str,
        action: str | None,
        **details: Any,
    ) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "provider": "github",
            "delivery_id": delivery_id,
            "type": event,
            "repository": github_repository,
        }
        if action is not None:
            metadata["action"] = action
        metadata.update(details)
        return metadata

    def _store_grouped(
        self,
        delivery_id: str,
        body_sha256: str,
        replay_key: str,
        repository: Any,
        group: str,
        build_payload: Callable[[], dict[str, Any] | None],
    ) -> dict[str, Any]:
        for _attempt in range(MAX_RECONCILE_ATTEMPTS):
            version = self.controller.store.group_version(repository.id, group)
            try:
                payload = build_payload()
            except EventRace:
                continue
            try:
                receipt = self.controller.store.ingest_event(
                    "github",
                    delivery_id,
                    body_sha256,
                    repository.id,
                    payload,
                    group=group,
                    expected_group_version=version,
                    replay_key=replay_key,
                )
            except EventConflict:
                duplicate = self._duplicate(delivery_id, body_sha256, replay_key, repository)
                if duplicate is not None:
                    return duplicate
                continue
            return self._receipt_status(receipt)
        raise EventRace("GitHub state changed repeatedly during event ingestion")

    def ingest(self, event: str, delivery_id: str, body: bytes) -> dict[str, Any]:
        if not isinstance(event, str) or _EVENT.fullmatch(event) is None:
            raise InvalidEvent("Invalid X-GitHub-Event value")
        if not isinstance(delivery_id, str) or _DELIVERY.fullmatch(delivery_id) is None:
            raise InvalidEvent("Invalid X-GitHub-Delivery value")
        payload = _json_object(body)
        repository, github_repository = self._repository(payload)
        body_sha256 = hashlib.sha256(body).hexdigest()
        replay_key = hashlib.sha256(event.encode("utf-8") + b"\0" + body).hexdigest()
        duplicate = self._duplicate(delivery_id, body_sha256, replay_key, repository)
        if duplicate is not None:
            return duplicate
        replay = self._replay(delivery_id, body_sha256, replay_key, repository)
        if replay is not None:
            return replay

        if event == "pull_request":
            return self._pull_request(
                payload,
                delivery_id,
                body_sha256,
                replay_key,
                repository,
                github_repository,
            )
        if event == "push":
            return self._push(
                payload,
                delivery_id,
                body_sha256,
                replay_key,
                repository,
                github_repository,
            )
        if event == "release":
            return self._release(
                payload,
                delivery_id,
                body_sha256,
                replay_key,
                repository,
                github_repository,
            )
        return self._ignored(
            delivery_id,
            body_sha256,
            replay_key,
            repository,
            f"unsupported GitHub event: {event}",
        )

    def _pull_request(
        self,
        payload: dict[str, Any],
        delivery_id: str,
        body_sha256: str,
        replay_key: str,
        repository: Any,
        github_repository: str,
    ) -> dict[str, Any]:
        action = _string(payload, "action", 64)
        number = _integer(payload, "number")
        webhook_pull = _object(payload, "pull_request")
        if _integer(webhook_pull, "number") != number:
            raise InvalidEvent("Pull request numbers in the payload do not match")
        _sha(_object(webhook_pull, "head"), "sha")
        webhook_base = _object(webhook_pull, "base")
        _string(webhook_base, "ref", 255)
        if not repository.events.pull_requests:
            return self._ignored(
                delivery_id,
                body_sha256,
                replay_key,
                repository,
                "pull request events are disabled",
            )
        if action not in _PULL_ACTIONS:
            return self._ignored(
                delivery_id,
                body_sha256,
                replay_key,
                repository,
                f"unsupported pull request action: {action}",
            )
        isolation = repository.isolation_policy
        if isolation is None or not isolation.required:
            raise InvalidEvent("Pull request events require centrally mandatory Docker isolation")

        group = _group("pull_request", str(number))

        def current() -> dict[str, Any] | None:
            pull = self.pull_request(github_repository, number)
            if _integer(pull, "number") != number:
                raise InvalidEvent("GitHub returned a different pull request")
            state = _string(pull, "state", 16)
            base = _object(pull, "base")
            base_repo = _string(_object(base, "repo"), "full_name", 200)
            if base_repo.casefold() != github_repository.casefold():
                raise InvalidEvent("GitHub returned a pull request from another repository")
            if _string(base, "ref", 255) != repository.base_branch:
                return None
            if state == "closed":
                return None
            if state != "open":
                raise InvalidEvent("GitHub returned an invalid pull request state")
            head = _object(pull, "head")
            source_sha = _sha(head, "sha")
            base_sha = _sha(base, "sha")
            source_ref = f"refs/pull/{number}/head"
            if self.revision(repository.source, source_ref) != source_sha:
                raise EventRace("Pull request ref changed during reconciliation")
            base_ref = f"refs/heads/{repository.base_branch}"
            if self.revision(repository.source, base_ref) != base_sha:
                raise EventRace("Pull request base changed during reconciliation")
            return {
                "config_sha256": self.controller.digest,
                "base_sha": base_sha,
                "source_sha": source_sha,
                "source_ref": source_ref,
                "stage": "pull-request",
                "mode": "check",
                "event": self._event_metadata(
                    "pull_request",
                    delivery_id,
                    github_repository,
                    action,
                    number=number,
                    head_ref=_string(head, "ref", 255),
                ),
            }

        return self._store_grouped(delivery_id, body_sha256, replay_key, repository, group, current)

    def _push(
        self,
        payload: dict[str, Any],
        delivery_id: str,
        body_sha256: str,
        replay_key: str,
        repository: Any,
        github_repository: str,
    ) -> dict[str, Any]:
        source_ref = _string(payload, "ref", 300)
        deleted = payload.get("deleted")
        if not isinstance(deleted, bool):
            raise InvalidEvent("GitHub push payload needs boolean member 'deleted'")
        base_ref = f"refs/heads/{repository.base_branch}"
        if source_ref != base_ref:
            return self._ignored(
                delivery_id,
                body_sha256,
                replay_key,
                repository,
                "push targets another branch",
            )
        if deleted:
            return self._ignored(delivery_id, body_sha256, replay_key, repository, "deleted push")
        if not repository.events.pushes:
            return self._ignored(
                delivery_id, body_sha256, replay_key, repository, "push events are disabled"
            )
        source_sha = _sha(payload, "after")
        group = _group("push", repository.base_branch)

        def current() -> dict[str, Any] | None:
            if self.revision(repository.source, source_ref) != source_sha:
                raise EventRace("Push ref changed during reconciliation")
            return {
                "config_sha256": self.controller.digest,
                "base_sha": source_sha,
                "source_sha": source_sha,
                "source_ref": source_ref,
                "stage": "merge",
                "mode": "check",
                "event": self._event_metadata(
                    "push", delivery_id, github_repository, None, ref=source_ref
                ),
            }

        return self._store_grouped(delivery_id, body_sha256, replay_key, repository, group, current)

    def _release(
        self,
        payload: dict[str, Any],
        delivery_id: str,
        body_sha256: str,
        replay_key: str,
        repository: Any,
        github_repository: str,
    ) -> dict[str, Any]:
        action = _string(payload, "action", 64)
        if action != "published":
            return self._ignored(
                delivery_id,
                body_sha256,
                replay_key,
                repository,
                f"unsupported release action: {action}",
            )
        if not repository.events.releases or repository.release is None:
            return self._ignored(
                delivery_id,
                body_sha256,
                replay_key,
                repository,
                "release events are disabled",
            )
        release = _object(payload, "release")
        tag = _string(release, "tag_name", 255)
        source_ref = f"refs/tags/{tag}"
        group = _group("release", tag)

        def current() -> dict[str, Any] | None:
            try:
                source_sha = self.revision(repository.source, source_ref)
                base_sha = self.revision(repository.source, f"refs/heads/{repository.base_branch}")
            except WorkspaceError as exc:
                raise InvalidEvent(f"Release source cannot be resolved: {exc}") from exc
            if source_sha != base_sha:
                return None
            return {
                "config_sha256": self.controller.digest,
                "base_sha": base_sha,
                "source_sha": source_sha,
                "source_ref": source_ref,
                "stage": "release",
                "mode": "release",
                "event": self._event_metadata(
                    "release", delivery_id, github_repository, action, tag=tag
                ),
            }

        receipt = self._store_grouped(
            delivery_id, body_sha256, replay_key, repository, group, current
        )
        if receipt["task_id"] is None:
            receipt["status"] = "ignored"
            receipt["reason"] = "release tag is not the current base commit"
        return receipt
