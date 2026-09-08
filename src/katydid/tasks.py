"""Validated task intent; provider events cannot imply repair or release authority."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from katydid.profile import Stage


class TaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    config_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    base_sha: str = Field(pattern=r"^(?:[a-f0-9]{40}|[a-f0-9]{64})$")
    source_sha: str | None = Field(default=None, pattern=r"^(?:[a-f0-9]{40}|[a-f0-9]{64})$")
    source_ref: str | None = None
    stage: Stage = "pull-request"
    mode: Literal["check", "repair", "release"] = "repair"
    event: dict[str, Any] | None = None

    @field_validator("source_ref")
    @classmethod
    def valid_ref(cls, value: str | None) -> str | None:
        if value is not None:
            from katydid.workspace import validate_source_ref

            validate_source_ref(value)
        return value

    @model_validator(mode="after")
    def coherent(self) -> "TaskRequest":
        if (self.source_ref is None) != (self.source_sha is None):
            raise ValueError("Source ref and SHA must be supplied together")
        if self.mode == "release" and self.stage != "release":
            raise ValueError("Release execution requires the release stage")
        if self.mode == "repair" and self.stage == "release":
            raise ValueError("A release task cannot repair its candidate")
        if self.source_ref and self.source_ref.startswith("refs/pull/"):
            if self.mode != "check" or self.stage != "pull-request":
                raise ValueError("Pull-request source refs permit only pull-request checks")
        return self
