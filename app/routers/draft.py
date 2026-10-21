"""Drafting an RFP response: the one-shot draft, the streamed draft, single sections, documents and questions."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from app import azure_clients, metrics
from app.agents import answers, lifecycle, runner, sections
from app.agents.normalize import normalize_section
from app.agents.untrusted import fence
from app.auth.access import require_rfp_access
from app.auth.entra import UserClaims, get_current_user
from app.auth.ratelimit import rate_limited
from app.config import Settings, get_settings
from app.outputs.docx_writer import generate_docx, parse_template_sections
from app.outputs.xlsx_writer import generate_xlsx
from app.schemas import AnswerRequest, DraftRequest, GenerateDocRequest, GenerateSectionRequest
from app.services.draft_pipeline import stream_draft
from app.services.drafts import load_rfp, publish_outputs, resolve_requirements, web_search_choice

logger = logging.getLogger(__name__)
router = APIRouter(tags=["agent"])

_STREAM_ERROR = "Draft generation failed. Check the server logs for details."


@router.post("/draft", dependencies=[Depends(rate_limited)])
def draft(
    req: DraftRequest,
    user: UserClaims = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    """Generate a full RFP response draft in one agent run (DOCX + XLSX)."""
    allow_web_search = web_search_choice(req.allow_web_search, settings)
    rfp = load_rfp(settings, user, req.rfp_id)
    requirements = resolve_requirements(req.requirements, rfp)

    start = time.time()
    draft_result = answers.run_draft(
        rfp_id=req.rfp_id,
        requirements_json=json.dumps(requirements, indent=2),
        client_id=req.client_id,
        project_id=req.project_id,
        solution_area=req.solution_area,
        tone=req.tone,
        user_oid=user.oid,
        user_groups=user.groups,
        allow_web_search=allow_web_search,
    )
    elapsed = time.time() - start
    logger.info("Draft generation took %.1fs for RFP %s", elapsed, req.rfp_id)

    if "error" in draft_result:
        raise HTTPException(status_code=502, detail=draft_result["error"])

    outputs = publish_outputs(
        settings,
        req.rfp_id,
        generate_docx(draft_result),
        generate_xlsx(draft_result.get("responses", []), requirements=requirements),
    )
    metrics.bump("drafts")

    return {"rfp_id": req.rfp_id, "draft": draft_result, "outputs": outputs, "elapsed_seconds": round(elapsed, 1)}


# ── Streaming draft (SSE) ────────────────────────────────


def _sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event)}\n\n"


@router.post("/draft/stream", dependencies=[Depends(rate_limited)])
def draft_stream(
    req: DraftRequest,
    user: UserClaims = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    """Stream RFP response generation – one SSE event per requirement.

    The response is bound to this request, so it is cut off by the platform's request timeout (240 s on Azure
    Container Apps). Use ``POST /drafts`` for anything that may take longer.
    """
    allow_web_search = web_search_choice(req.allow_web_search, settings)
    rfp = load_rfp(settings, user, req.rfp_id)
    requirements = resolve_requirements(req.requirements, rfp)

    def event_generator():
        try:
            for event in stream_draft(settings, req, requirements, user, allow_web_search):
                yield _sse(event)
        except Exception as exc:
            logger.exception("Draft stream failed for rfp_id=%s", req.rfp_id)
            yield _sse({"type": "error", "message": exc.detail if isinstance(exc, HTTPException) else _STREAM_ERROR})

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Single section / document ────────────────────────────


@router.post("/draft/generate-section", dependencies=[Depends(rate_limited)])
def generate_section(req: GenerateSectionRequest, user: UserClaims = Depends(get_current_user)):
    """Generate content for a single template section using the provided prompt and responses."""
    agent_id = lifecycle.ensure_agent(lifecycle.DRAFT_AGENT_NAME)

    responses_context = ""
    if req.responses:
        responses_context = "\n\n--- Requirement Responses (use these as source material) ---\n\n" + fence(
            "RESPONSES",
            "\n\n".join(
                f"### [{r.get('requirement_id', '?')}] (Compliance: {r.get('meets', '?')})\n"
                f"Requirement: {r.get('requirement_text', '')[:200]}\n"
                f"Response: {r.get('answer_markdown', '')}"
                for r in req.responses
            ),
        )

    full_prompt = (
        req.prompt
        + responses_context
        + (
            f"\n\nReturn ONLY a JSON object:\n"
            f"{{\"section_name\": {json.dumps(req.section_name)}, "
            f"\"narrative_markdown\": \"<your section text>\"}}"
        )
    )

    agent_result = runner.run_agent_prompt(agent_id, full_prompt)
    if not agent_result.completed:
        raise HTTPException(status_code=502, detail=f"Section generation failed: {agent_result.status}")

    safe, _ = runner.post_generation_guard(agent_result.text)
    try:
        return normalize_section(runner.parse_json_object(safe), section_name=req.section_name)
    except (ValueError, IndexError):
        return {"section_name": req.section_name, "narrative_markdown": safe}


@router.post("/draft/generate-doc", dependencies=[Depends(rate_limited)])
def generate_doc(
    req: GenerateDocRequest,
    user: UserClaims = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    """Generate the final DOCX and XLSX from the responses (and section narratives) the UI collected.

    With the per-section approach and narratives already available they are reused; otherwise the
    responses are grouped into template sections and a narrative is written for each.
    """
    responses = req.responses
    if not responses and not req.section_narratives:
        raise HTTPException(status_code=400, detail="No responses or section narratives provided")

    require_rfp_access(azure_clients.container(settings, settings.storage_rfp_container), req.rfp_id, user, settings)

    if req.approach == "per_section" and req.section_narratives:
        sections_result = req.section_narratives
        section_map = {
            s.get("section_name", f"Section {i + 1}"): s.get("requirement_ids", [])
            for i, s in enumerate(sections_result)
        }
    else:
        agent_id = lifecycle.ensure_agent(lifecycle.DRAFT_AGENT_NAME)
        section_names = sections.section_names_for(parse_template_sections())
        section_map = sections.classify_sections(
            agent_id,
            section_names,
            [(r.get("requirement_id", "?"), r.get("requirement_text", r.get("answer_markdown", ""))) for r in responses],
        )
        resp_by_id = {r.get("requirement_id", ""): r for r in responses}
        jobs = [
            {
                "section_name": section_name,
                "req_ids": req_ids,
                "section_answers": [resp_by_id[rid] for rid in req_ids if rid in resp_by_id],
                "tone": req.tone,
                "agent_id": agent_id,
            }
            for section_name, req_ids in section_map.items()
        ]
        sections_result = list(
            sections.write_sections(
                [job for job in jobs if job["section_answers"]],
                sections.generate_section_narrative_from_answers,
                workers=1,
            )
        )

    executive, overview = sections.summaries_from_sections(sections_result, "See sections.")
    draft_result = {
        "executive_summary": executive,
        "solution_overview": overview,
        "section_narratives": sections_result,
        "responses": responses,
    }
    outputs = publish_outputs(settings, req.rfp_id, generate_docx(draft_result), generate_xlsx(responses, []))
    return {"sections": sections_result, "section_map": section_map, "outputs": outputs}


# ── Single question ──────────────────────────────────────


@router.post("/answer", dependencies=[Depends(rate_limited)])
def answer(
    req: AnswerRequest,
    user: UserClaims = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    """Answer a single question using the grounded agent."""
    # req.question was redacted when the request was validated
    result = answers.run_single_answer(
        question=req.question,
        client_id=req.client_id,
        project_id=req.project_id,
        solution_area=req.solution_area,
        user_oid=user.oid,
        user_groups=user.groups,
        allow_web_search=web_search_choice(req.allow_web_search, settings),
    )
    metrics.bump("answers")
    return result
