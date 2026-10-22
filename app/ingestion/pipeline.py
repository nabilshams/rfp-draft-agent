"""The one place where text is redacted before it can reach a model.

Everything that comes from outside the application, or from documents of unknown provenance, passes
through here:

* uploaded RFPs (:func:`extract_requirements`, :func:`redacted_document_text`)
* request fields supplied by clients: questions, context, requirements, prompts and earlier answers
  (:data:`RedactedText` and :data:`RedactedRecords`, which plug into the request models so no endpoint
  can forget it)
* knowledge-base chunks at retrieval time (see ``app.agents.kb_search``), so a rule or customer name added
  after a document was indexed still applies to it

Redaction is idempotent, so applying it more than once is harmless.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import AfterValidator

from app.ingestion.redaction import get_redaction_engine
from app.ingestion.rfp_extract import (
    RFPExtractionResult,
    _extract_text_from_csv,
    _extract_text_from_excel,
    extract_from_file,
    extract_text,
)

# Keys whose values are identifiers, enumerations or URLs, not free text: redacting them would break
# lookups and citation checks.
_PRESERVED_KEYS = frozenset(
    {"id", "requirement_id", "requirement_ids", "source_url", "url", "meets", "priority", "category", "section_name"}
)


def redact_text(text: str) -> str:
    return get_redaction_engine().redact(text).sanitized_text


def redact_records(value: Any) -> Any:
    """Redact the free-text strings inside nested dicts and lists, leaving identifiers and URLs alone."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact_records(item) for item in value]
    if isinstance(value, dict):
        return {k: v if k in _PRESERVED_KEYS else redact_records(v) for k, v in value.items()}
    return value


# Request-model field types: validation redacts the value, so handlers only ever see clean data.
RedactedText = Annotated[str, AfterValidator(redact_text)]
RedactedRecords = Annotated[list[dict[str, Any]], AfterValidator(redact_records)]


def extract_requirements(filename: str, data: bytes) -> RFPExtractionResult:
    """Extract an RFP's structure with sensitive content redacted (every field of the result)."""
    return redact_extraction(extract_from_file(filename, data))


def redact_extraction(extraction: RFPExtractionResult) -> RFPExtractionResult:
    for requirement in extraction.requirements:
        requirement.text = redact_text(requirement.text)
        requirement.category = redact_text(requirement.category)
        requirement.source_section = redact_text(requirement.source_section)
        requirement.keywords = [redact_text(k) for k in requirement.keywords]
    extraction.questions = [redact_text(q) for q in extraction.questions]
    extraction.evaluation_criteria = [redact_text(c) for c in extraction.evaluation_criteria]
    extraction.sections = redact_records(extraction.sections)
    extraction.response_instructions = redact_records([extraction.response_instructions])[0]
    return extraction


def redacted_document_text(filename: str, data: bytes) -> str:
    """The plain text of an RFP file, redacted (used when a model extracts the requirements)."""
    lower = filename.lower()
    if lower.endswith((".xlsx", ".xls")):
        text = _extract_text_from_excel(data)
    elif lower.endswith(".csv"):
        text = _extract_text_from_csv(data)
    else:
        text = extract_text(filename, data)
    return redact_text(text)
