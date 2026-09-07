"""Strict repository profiles and deterministic plans. Loading never executes code."""

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path, PureWindowsPath
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from yaml.nodes import MappingNode

Stage = Literal["pull-request", "merge", "nightly"]
Identifier = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")]
MAX_PROFILE_BYTES = 1024 * 1024


class ProfileError(ValueError):
    """An invalid profile or an unresolvable test plan."""


def default_stages() -> list[Stage]:
    return ["pull-request"]


class UniqueSafeLoader(yaml.SafeLoader):
    """Reject duplicate keys rather than silently replacing earlier policy fields."""

    def construct_mapping(self, node: MappingNode, deep: bool = False) -> dict[Any, Any]:
        result: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str):
                raise ProfileError("Profile mapping keys must be strings")
            if key in result:
                raise ProfileError(f"Duplicate profile key: {key}")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


class Check(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    id: Identifier
    kind: Literal["command", "test"]
    argv: list[str] = Field(min_length=1, max_length=256)
    working_directory: str = "."
    timeout_seconds: int = Field(default=300, ge=1, le=3600)
    stages: list[Stage] = Field(default_factory=default_stages, min_length=1)
    required: bool = True

    @field_validator("argv")
    @classmethod
    def valid_argv(cls, value: list[str]) -> list[str]:
        if not value[0].strip() or any("\x00" in arg for arg in value):
            raise ValueError("argv needs an executable and cannot contain NUL characters")
        return value

    @field_validator("working_directory")
    @classmethod
    def relative_directory(cls, value: str) -> str:
        windows = PureWindowsPath(value)
        if not value or "\x00" in value or "\\" in value or windows.drive or windows.root:
            raise ValueError("working_directory must be relative and use forward slashes")
        return value

    @field_validator("stages")
    @classmethod
    def unique_stages(cls, value: list[Stage]) -> list[Stage]:
        if len(value) != len(set(value)):
            raise ValueError("stages cannot contain duplicates")
        return value


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: Literal[1]
    repository: Identifier
    owner: str = Field(min_length=1, max_length=200)
    checks: list[Check] = Field(min_length=1, max_length=100)

    @field_validator("schema_version", mode="before")
    @classmethod
    def integer_version(cls, value: Any) -> Any:
        if type(value) is not int:
            raise ValueError("schema_version must be integer 1")
        return value

    @field_validator("owner")
    @classmethod
    def nonblank_owner(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("owner cannot be blank")
        return value

    @field_validator("checks")
    @classmethod
    def unique_checks(cls, value: list[Check]) -> list[Check]:
        if len(value) != len({check.id for check in value}):
            raise ValueError("Check IDs must be unique")
        return value


@dataclass(frozen=True)
class PlannedCheck:
    id: str
    kind: str
    argv: tuple[str, ...]
    working_directory: str
    timeout_seconds: int
    required: bool


@dataclass(frozen=True)
class Plan:
    schema_version: int
    repository: str
    owner: str
    stage: str
    root: str
    profile_path: str
    profile_sha256: str
    checks: tuple[PlannedCheck, ...]
    excluded: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)


def load_profile(path: Path) -> tuple[Profile, str]:
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_PROFILE_BYTES + 1)
        if len(raw) > MAX_PROFILE_BYTES:
            raise ProfileError("Profile exceeds the 1 MiB limit")
        source = raw.decode("utf-8")
        # No aliases, anchors, or merge keys: avoid hidden overrides and expansion attacks.
        for token in yaml.scan(source):
            if isinstance(token, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken)):
                raise ProfileError("YAML anchors and aliases are not supported")
        data = yaml.load(source, Loader=UniqueSafeLoader)
        profile = Profile.model_validate(data)
    except (OSError, UnicodeError, yaml.YAMLError, ValidationError, RecursionError) as exc:
        raise ProfileError(f"Cannot load profile {path}: {exc}") from exc
    return profile, hashlib.sha256(raw).hexdigest()


def make_plan(path: Path, stage: Stage, root: Path | None = None) -> Plan:
    path = path.resolve()
    profile, digest = load_profile(path)
    base = (root or path.parent).resolve()
    if not base.is_dir():
        raise ProfileError(f"Repository root is not a directory: {base}")
    checks: list[PlannedCheck] = []
    excluded: list[str] = []
    for check in profile.checks:
        working_directory = (base / check.working_directory).resolve()
        if not working_directory.is_relative_to(base) or not working_directory.is_dir():
            raise ProfileError(f"Check {check.id}: working directory must exist within {base}")
        if stage not in check.stages:
            excluded.append(f"{check.id}: not configured for stage {stage}")
            continue
        checks.append(
            PlannedCheck(
                check.id,
                check.kind,
                tuple(check.argv),
                str(working_directory),
                check.timeout_seconds,
                check.required,
            )
        )
    if not checks or not any(check.required for check in checks):
        raise ProfileError(f"Stage {stage} must select at least one required check")
    return Plan(
        1,
        profile.repository,
        profile.owner,
        stage,
        str(base),
        str(path),
        digest,
        tuple(checks),
        tuple(excluded),
    )
