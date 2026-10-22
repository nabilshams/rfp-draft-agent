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
from app.telemetry import spans

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
    job_id: str = "",
) -> Iterator[dict[str, Any]]:
    """``start``, the approach's events, then ``complete`` once the documents are stored.

    Raises if generation or storing the documents fails (after the events produced so far). The whole draft is one
    trace (a ``draft`` span carrying *job_id*, totals of tokens and estimated cost) so each requirement's stages can be
    followed from the job.
    """
    draft = spans.DraftTrace(
        "draft",
        spans.meter_from_settings(settings),
        job__id=job_id,
        rfp__id=req.rfp_id,
        approach=req.approach,
        requirements__total=len(requirements),
        web_search=allow_web_search,
    )
    outcome = "cancelled"  # what a draft that is closed before it finishes (cancelled, client gone) ends as
    responses: list[dict[str, Any]] = []
    try:
        yield {"type": "start", "total": len(requirements), "approach": req.approach}

        narratives: list[dict[str, Any]] = []
        for event in draft.iterate(draft_events(req, requirements, user, allow_web_search, guidance)):
            if event["type"] == "response":
                logger.info("[RFP-TRACE] response requirement_id=%s", event["data"].get("requirement_id"))
                responses.append(event["data"])
            elif event["type"] == "section_narrative":
                narratives.append(event["data"])
            yield event

        with draft.step(), spans.stage("outputs") as outputs_span:
            if req.approach in ("map_reduce", "per_section"):
                executive, overview = sections.summaries_from_sections(narratives, "See section narratives.")
            else:
                executive, overview = "See individual requirement responses.", "Responses generated per requirement."
            draft_result = {"executive_summary": executive, "solution_overview": overview, "responses": responses}
            docx_bytes, xlsx_bytes = generate_docx(draft_result), generate_xlsx(responses, requirements)
            outputs_span.set(outputs__bytes=len(docx_bytes) + len(xlsx_bytes))
            outputs = publish_outputs(settings, req.rfp_id, docx_bytes, xlsx_bytes)
        metrics.bump("drafts")
        outcome = "ok"
        yield {"type": "complete", "outputs": outputs}
    except Exception as exc:
        outcome = "error"
        draft.stage.set(error__type=type(exc).__name__)
        raise
    finally:
        draft.finish(outcome, requirements__done=len(responses))
