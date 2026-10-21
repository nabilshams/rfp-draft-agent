"""Request bodies."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.ingestion.pipeline import RedactedRecords, RedactedText
from app.validation import Filename, KbBlobName, RfpId, Segment, Tone


class ExtractRequest(BaseModel):
    rfp_id: RfpId
    filename: Filename


class DraftRequest(BaseModel):
    rfp_id: RfpId
    client_id: Segment
    project_id: Segment
    solution_area: Segment | None = None
    tone: Tone = "professional"
    approach: str = "per_requirement"  # "per_requirement" or "per_section" or "map_reduce"
    context: RedactedText = ""  # common context applied to every requirement
    # Client-supplied requirements are redacted on arrival: they reach the model like any other input.
    requirements: RedactedRecords = Field(default_factory=list)
    # Web grounding sends the search query outside the Azure compliance boundary, so it is off unless asked for.
    allow_web_search: bool = False


class AnswerRequest(BaseModel):
    question: RedactedText
    client_id: Segment
    project_id: Segment
    solution_area: Segment | None = None
    allow_web_search: bool = False


class IngestRequest(BaseModel):
    """Re-ingest a KB document you own. Access comes from the blob's recorded owner and shared groups."""

    model_config = ConfigDict(extra="forbid")

    blob_name: KbBlobName
    redaction_level: str = "standard"


class GenerateDocRequest(BaseModel):
    rfp_id: RfpId
    responses: RedactedRecords
    section_narratives: RedactedRecords = []
    approach: str = "per_requirement"
    client_id: Segment = "demo-client"
    project_id: Segment = "demo-project"
    solution_area: Segment = "general"
    tone: Tone = "professional"


class GenerateSectionRequest(BaseModel):
    section_name: str = Field(min_length=1, max_length=200)
    prompt: RedactedText
    responses: RedactedRecords = []
    tone: Tone = "professional"
