"""Central, operator-owned repository and autonomy policy."""

import hashlib
from pathlib import Path, PureWindowsPath
from typing import Any, Literal, cast

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from katydid.profile import (
    Check,
    Identifier,
    ImageDigest,
    Isolation,
    Plan,
    ProfileError,
    Stage,
    UniqueSafeLoader,
)


def relative_file(value: str) -> str:
    path = PureWindowsPath(value)
    parts = value.split("/")
    if (
        not value
        or "\\" in value
        or "\x00" in value
        or path.drive
        or path.root
        or any(part in ("", ".", "..", ".git") for part in parts)
        or any(part.startswith(".env") for part in parts)
    ):
        raise ValueError(f"Expected a nonsensitive relative file path: {value}")
    return value


class AIConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    provider: Literal["codex"] = "codex"
    model: str = "gpt-5.6-sol"
    reasoning: Literal["low", "medium", "high"] = "high"
    command: list[str] | None = None
    timeout_seconds: int = Field(default=180, ge=10, le=900)
    max_calls_per_task: int = Field(default=6, ge=2, le=20)
    max_context_bytes: int = Field(default=180000, ge=1000, le=500000)

    @field_validator("command")
    @classmethod
    def command_array(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and (not value or any(not arg or "\x00" in arg for arg in value)):
            raise ValueError("AI command must be a nonempty executable argument array")
        return value


class DeliveryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    mode: Literal["none", "local", "github"] = "none"
    github_repository: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"
    )
    auto_merge: bool = False

    @model_validator(mode="after")
    def coherent(self) -> "DeliveryConfig":
        if self.mode == "github" and not self.github_repository:
            raise ValueError("GitHub delivery needs github_repository")
        if self.mode == "none" and self.auto_merge:
            raise ValueError("auto_merge requires a delivery mode")
        return self


class ReleaseConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    deploy: Check
    health: Check
    rollback: Check

    @model_validator(mode="after")
    def command_hooks(self) -> "ReleaseConfig":
        for hook in (self.deploy, self.health, self.rollback):
            if hook.kind != "command" or not hook.required:
                raise ValueError("Release hooks must be required command checks")
        return self


class IsolationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    required: bool = True
    images: list[ImageDigest] = Field(min_length=1, max_length=50)
    files: list[str] = Field(min_length=1, max_length=500)
    namespace: Identifier = "katydid"
    max_cpus: float = Field(default=1.0, ge=0.1, le=8)
    max_memory_mb: int = Field(default=512, ge=64, le=8192)
    max_pids: int = Field(default=128, ge=16, le=1024)
    max_tmpfs_mb: int = Field(default=64, ge=16, le=1024)

    @field_validator("files")
    @classmethod
    def source_files(cls, value: list[str]) -> list[str]:
        return Isolation.source_files(value)


class GitHubEvents(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    github_repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    pull_requests: bool = True
    pushes: bool = True
    releases: bool = False


class RepositoryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    id: Identifier
    source: str = Field(min_length=1)
    base_branch: str = "main"
    profile: str = "quality.yaml"
    context_paths: list[str] = Field(min_length=1, max_length=100)
    editable_paths: list[str] = Field(default_factory=list, max_length=50)
    requirements: str = Field(min_length=1, max_length=20000)
    required_checks: dict[str, Literal["command", "test"]] = Field(min_length=1)
    required_checks_by_stage: dict[Stage, dict[str, Literal["command", "test"]]] = Field(
        default_factory=dict
    )
    repair_attempts: int = Field(default=2, ge=1, le=5)
    delivery: DeliveryConfig = Field(default_factory=DeliveryConfig)
    release: ReleaseConfig | None = None
    isolation_policy: IsolationPolicy | None = None
    events: GitHubEvents | None = None

    @field_validator("profile")
    @classmethod
    def profile_path(cls, value: str) -> str:
        return relative_file(value)

    @field_validator("context_paths", "editable_paths")
    @classmethod
    def file_paths(cls, value: list[str]) -> list[str]:
        for path in value:
            relative_file(path)
        if len(set(value)) != len(value):
            raise ValueError("File paths cannot repeat")
        return value

    @field_validator("base_branch")
    @classmethod
    def branch_name(cls, value: str) -> str:
        if (
            not value
            or value.startswith("-")
            or ".." in value
            or "@{" in value
            or any(char in value for char in " ~^:?*[\\\x00")
        ):
            raise ValueError("Invalid base branch")
        return value

    @model_validator(mode="after")
    def protected_profile(self) -> "RepositoryConfig":
        if self.profile in self.editable_paths:
            raise ValueError("AI cannot edit the execution profile")
        if not set(self.editable_paths).issubset(self.context_paths):
            raise ValueError("editable_paths must be explicitly present in context_paths")
        if self.release and not self.delivery.auto_merge:
            raise ValueError("Release hooks require automatic merge of the verified candidate")
        for checks in self.required_checks_by_stage.values():
            for name, kind in checks.items():
                if name in self.required_checks and self.required_checks[name] != kind:
                    raise ValueError("Stage requirements cannot change a mandatory check kind")
        if self.events and self.events.releases and not self.release:
            raise ValueError("Release events require centrally configured release hooks")
        return self


class FleetConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal[1]
    state_directory: str = ".katydid/control"
    ai: AIConfig = Field(default_factory=AIConfig)
    repositories: list[RepositoryConfig] = Field(min_length=1)

    @field_validator("schema_version", mode="before")
    @classmethod
    def version_integer(cls, value: Any) -> Any:
        if type(value) is not int:
            raise ValueError("schema_version must be integer 1")
        return value

    @model_validator(mode="after")
    def unique_repositories(self) -> "FleetConfig":
        if len({repo.id for repo in self.repositories}) != len(self.repositories):
            raise ValueError("Repository IDs must be unique")
        identities = [
            repo.events.github_repository.casefold() for repo in self.repositories if repo.events
        ]
        if len(set(identities)) != len(identities):
            raise ValueError("GitHub event identities must be unique")
        return self

    def repository(self, name: str) -> RepositoryConfig:
        for repository in self.repositories:
            if repository.id == name:
                return repository
        raise ProfileError(f"Repository is not registered: {name}")


def load_fleet(path: Path) -> tuple[FleetConfig, str]:
    path = path.resolve()
    try:
        with path.open("rb") as stream:
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("Fleet configuration exceeds 1 MiB")
        source = raw.decode("utf-8")
        for token in yaml.scan(source):
            if isinstance(token, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken)):
                raise ValueError("Fleet aliases and anchors are not supported")
        config = FleetConfig.model_validate(yaml.load(source, Loader=UniqueSafeLoader))
        repositories = []
        source_keys = set()
        for repository in config.repositories:
            location = repository.source
            if location.startswith("https://github.com/"):
                if any(char in location for char in "?#@\x00"):
                    raise ValueError("GitHub source must be a credential-free repository URL")
                if (
                    repository.events
                    and location.removeprefix("https://github.com/").removesuffix(".git").casefold()
                    != repository.events.github_repository.casefold()
                ):
                    raise ValueError("GitHub event repository must match its registered source")
            elif "://" in location:
                raise ValueError("Only local Git paths and HTTPS GitHub repositories are supported")
            else:
                location = str((path.parent / location).resolve())
            source_key = location.casefold().removesuffix(".git")
            if source_key in source_keys:
                raise ValueError("A Git source can only be registered once; combine its checks")
            source_keys.add(source_key)
            repositories.append(repository.model_copy(update={"source": location}))
        state = str((path.parent / config.state_directory).resolve())
        config = config.model_copy(update={"state_directory": state, "repositories": repositories})
        return config, hashlib.sha256(raw).hexdigest()
    except (OSError, ValueError, yaml.YAMLError, ValidationError, RecursionError) as exc:
        raise ProfileError(f"Cannot load fleet configuration: {exc}") from exc


def enforce_policy(repository: RepositoryConfig, plan: Plan) -> None:
    isolation_policy = repository.isolation_policy
    if isolation_policy is not None:
        isolation = plan.isolation
        if isolation is None:
            if isolation_policy.required:
                raise ProfileError("Central policy requires Docker isolation")
        elif (
            isolation.adapter != "docker"
            or isolation.image not in isolation_policy.images
            or not set(isolation.files).issubset(isolation_policy.files)
            or isolation.namespace != isolation_policy.namespace
            or isolation.cpus > isolation_policy.max_cpus
            or isolation.memory_mb > isolation_policy.max_memory_mb
            or isolation.pids_limit > isolation_policy.max_pids
            or isolation.tmpfs_mb > isolation_policy.max_tmpfs_mb
        ):
            raise ProfileError(
                "Isolation exceeds central image, source, namespace, or resource policy"
            )
    selected = {check.id: check for check in plan.checks}
    required = {
        **repository.required_checks,
        **repository.required_checks_by_stage.get(cast(Stage, plan.stage), {}),
    }
    for check_id, kind in required.items():
        check = selected.get(check_id)
        if check is None or not check.required or check.kind != kind:
            raise ProfileError(f"Central policy requires {check_id} as a required {kind} check")
