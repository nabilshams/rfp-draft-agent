"""A whole streamed draft: the events of the chosen approach, then the generated documents.

Used by the request-bound ``POST /draft/stream`` and by the background jobs behind ``POST /drafts``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

from app import metrics
from app.agents import sections, strategies
from app.auth.entra import UserClaims
from app.config import Settings
from app.outputs.docx_writer import generate_docx, parse_template_sections
from app.outputs.xlsx_writer import generate_xlsx
from app.schemas import DraftRequest
from app.services.drafts import publish_outputs

logger = logging.getLogger(__name__)


def draft_events(
    req: DraftRequest,
    requirements: list[dict[str, Any]],
    user: UserClaims,
    allow_web_search: bool,
    guidance: str = "",
) -> Iterator[dict[str, Any]]:
    """The events of the chosen approach, in the shape the UI consumes."""
    common = {
        "requirements": requirements,
        "client_id": req.client_id,
        "project_id": req.project_id,
        "solution_area": req.solution_area,
        "tone": req.tone,
        "user_oid": user.oid,
        "user_groups": user.groups,
        "user_context": req.context,
        "allow_web_search": allow_web_search,
        "guidance": guidance,
    }
    if req.approach == "map_reduce":
        yield from strategies.stream_map_reduce(template_sections=parse_template_sections(), **common)
    elif req.approach == "per_section":
        for event in strategies.stream_per_section(template_sections=parse_template_sections(), **common):
            # The compliance matrix entries are what the UI shows as responses for this approach
            yield {"type": "response", "data": event["data"]} if event["type"] == "requirement_compliance" else event
    else:
        for resp in strategies.stream_per_requirement(**common):
            yield {"type": "response", "data": resp}


def stream_draft(
    settings: Settings,
    req: DraftRequest,
    requirements: list[dict[str, Any]],
    user: UserClaims,
    allow_web_search: bool,
    guidance: str = "",
) -> Iterator[dict[str, Any]]:
    """``start``, the approach's events, then ``complete`` once the documents are stored.

    Raises if generation or storing the documents fails (after the events produced so far).
    """
    yield {"type": "start", "total": len(requirements), "approach": req.approach}

    responses: list[dict[str, Any]] = []
    narratives: list[dict[str, Any]] = []
    for event in draft_events(req, requirements, user, allow_web_search, guidance):
        if event["type"] == "response":
            logger.info("[RFP-TRACE] response requirement_id=%s", event["data"].get("requirement_id"))
            responses.append(event["data"])
        elif event["type"] == "section_narrative":
            narratives.append(event["data"])
        yield event

    if req.approach in ("map_reduce", "per_section"):
        executive, overview = sections.summaries_from_sections(narratives, "See section narratives.")
    else:
        executive, overview = "See individual requirement responses.", "Responses generated per requirement."
    draft_result = {"executive_summary": executive, "solution_overview": overview, "responses": responses}
    outputs = publish_outputs(settings, req.rfp_id, generate_docx(draft_result), generate_xlsx(responses, requirements))
    metrics.bump("drafts")
    yield {"type": "complete", "outputs": outputs}
