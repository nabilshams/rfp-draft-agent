"""Request bodies for the project ledger API. Text a person writes is redacted on arrival like every other input."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.ingestion.pipeline import RedactedRecords, RedactedText
from app.validation import Filename, RfpId

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Short = Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)]
Oid = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._@-]{0,64}$")]
Status = Literal["extracted", "drafted", "edited", "approved"]
Meets = Literal["Yes", "Partial", "No"]


class ProjectCreate(BaseModel):
    """Start a project from an uploaded RFP (its requirements are extracted) or from requirements you supply."""

    model_config = ConfigDict(extra="forbid")

    name: Name
    rfp_id: RfpId | None = None
    filename: Filename | None = None  # which file of the RFP, when it has several
    requirements: RedactedRecords | None = None
    share_with_groups: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def _needs_a_source(self) -> ProjectCreate:
        if not self.rfp_id and not self.requirements:
            raise ValueError("give an rfp_id or a list of requirements")
        return self


class ProjectUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Name | None = None
    share_with_groups: list[str] | None = Field(default=None, max_length=20)


class RequirementUpdate(BaseModel):
    """Fields left out are not changed. ``assignee`` set to an empty string clears the assignment."""

    model_config = ConfigDict(extra="forbid")

    text: Annotated[RedactedText, StringConstraints(min_length=1, max_length=5000)] | None = None
    category: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)] | None = None
    priority: Literal["mandatory", "preferred", "standard"] | None = None
    status: Status | None = None
    assignee: Oid | None = None
    assignee_name: Short | None = None
    expected_rev: int | None = Field(default=None, ge=1, description="The `rev` you last saw; a mismatch is a 409.")

    def changes(self) -> dict[str, object]:
        data = self.model_dump(exclude_unset=True, exclude={"expected_rev"})
        if "assignee" in data and not data["assignee"]:
            data["assignee_name"] = ""  # clearing the assignee clears the name too
        return {k: v for k, v in data.items() if v is not None}


class AnswerCreate(BaseModel):
    """A new version of an answer: write one (``answer_markdown``), or make an earlier version current again."""

    model_config = ConfigDict(extra="forbid")

    answer_markdown: Annotated[RedactedText, StringConstraints(max_length=50000)] | None = None
    meets: Meets | None = None
    evidence: RedactedRecords = Field(default_factory=list, max_length=50)
    assumptions: list[RedactedText] = Field(default_factory=list, max_length=50)
    risks: list[RedactedText] = Field(default_factory=list, max_length=50)
    followups: list[RedactedText] = Field(default_factory=list, max_length=50)
    restore_version: int | None = Field(default=None, ge=1)
    expected_rev: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _one_or_the_other(self) -> AnswerCreate:
        writing = self.answer_markdown is not None
        if writing == (self.restore_version is not None):
            raise ValueError("give either answer_markdown or restore_version")
        return self

    def content(self) -> dict[str, object]:
        data = self.model_dump(exclude={"restore_version", "expected_rev"})
        if data.get("meets") is None:
            data.pop("meets", None)
        return data
