"""Generating answers: a full draft in one run, one requirement at a time, or a single free-form question."""

from __future__ import annotations

import json
import logging
from typing import Any

from app.agents import kb_search, lifecycle, runner
from app.agents.kb_search import RetrievedContext
from app.agents.normalize import normalize_answer, normalize_draft
from app.agents.untrusted import fence, guidance_block

logger = logging.getLogger(__name__)

_COMPLIANCE_FIELDS = (
    '- "meets": "Yes", "Partial", or "No"\n'
    '- "evidence": array of {"title", "source_url", "quote"}\n'
    '- "assumptions": array of strings\n'
    '- "risks": array of strings\n'
    '- "followups": array of strings\n'
)


def parse_requirements(requirements_json: str) -> list[dict[str, Any]]:
    """The requirement dicts in a JSON array, or an empty list if it isn't one."""
    try:
        parsed = json.loads(requirements_json)
    except json.JSONDecodeError:
        return []
    return [r for r in parsed if isinstance(r, dict)] if isinstance(parsed, list) else []


def error_answer(requirement: dict[str, Any], *, message: str, risk: str, kb_evidence_found: bool = False) -> dict[str, Any]:
    """The response shown for a requirement whose generation failed."""
    return {
        "requirement_id": requirement.get("id", "?"),
        "requirement_text": requirement.get("text", ""),
        "answer_markdown": message,
        "meets": "No",
        "evidence": [],
        "assumptions": [],
        "risks": [risk],
        "followups": [],
        "kb_evidence_found": kb_evidence_found,
    }


def exception_answer(requirement: dict[str, Any], exc: BaseException) -> dict[str, Any]:
    return error_answer(requirement, message=f"Error: {exc}", risk=str(exc))


def run_draft(
    *,
    rfp_id: str,
    requirements_json: str,
    client_id: str,
    project_id: str,
    solution_area: str | None = None,
    tone: str = "professional",
    user_oid: str,
    user_groups: list[str] | None = None,
    allow_web_search: bool = False,
    guidance: str = "",
) -> dict[str, Any]:
    """Submit the full RFP to the draft agent in one run and return the draft JSON."""

    # Retrieve knowledge-base content per requirement, then share the best of it across the draft
    requirements = parse_requirements(requirements_json) or [{"text": requirements_json[:1000]}]
    per_requirement = kb_search.retrieve_for_requirements(
        requirements,
        client_id=client_id,
        project_id=project_id,
        solution_area=solution_area,
        user_oid=user_oid,
        user_groups=user_groups,
    )
    context = kb_search.merge_contexts(per_requirement)
    evidence_found = {str(r.get("id", "")): c.found for r, c in zip(requirements, per_requirement)}

    agent_id = lifecycle.ensure_agent(lifecycle.DRAFT_AGENT_NAME)
    logger.info("Using draft agent %s for RFP %s", agent_id, rfp_id)

    user_message = (
        f"Draft a complete RFP response. Tone: {tone}.\n\n"
        f"CRITICAL: You MUST produce exactly one response object for EACH requirement listed below. "
        f"Use the EXACT requirement_id from the input (e.g. F1, F2, F3...). "
        f"Do NOT skip, merge, or invent requirements.\n\n"
        f"Return a JSON object with:\n"
        f"- \"executive_summary\": a brief executive summary\n"
        f"- \"solution_overview\": a solution overview paragraph\n"
        f"- \"responses\": an array with one object per requirement, each containing:\n"
        f"  - \"requirement_id\": the exact id from the requirement\n"
        f"  - \"requirement_text\": the original requirement text (copied verbatim)\n"
        f"  - \"answer_markdown\": your detailed response\n"
        f"  - \"meets\": \"Yes\", \"Partial\", or \"No\"\n"
        f"  - \"evidence\": array of {{\"title\", \"source_url\", \"quote\"}}\n"
        f"  - \"assumptions\": array of strings\n"
        f"  - \"risks\": array of strings\n"
        f"  - \"followups\": array of strings\n\n"
        f"Use the following retrieved sources to ground your answers:\n\n"
        f"{context.block}\n\n"
        f"{guidance_block(guidance)}"
        f"Here are the requirements to respond to:\n{fence('REQUIREMENTS', requirements_json)}"
    )

    result = runner.run_agent_prompt(agent_id, user_message, web_search=allow_web_search)

    if not result.completed:
        logger.error("Agent run failed: %s – %s", result.status, result.last_error)
        return {"error": f"Agent run ended with status: {result.status}"}

    if not result.has_response:
        return {"error": "No assistant response received"}

    safe_text, violations = runner.post_generation_guard(result.text)
    if violations:
        logger.warning("Draft contained %d sensitive patterns – redacted", len(violations))

    # Only sources that were retrieved for this draft (or returned by web grounding) may be cited.
    allowed_urls = context.urls | result.annotation_urls

    try:
        draft = kb_search.sanitize_citations(normalize_draft(runner.parse_json_object(safe_text)), allowed_urls)
    except (ValueError, IndexError):
        draft = {"raw_response": kb_search.sanitize_citations(safe_text, allowed_urls), "parse_error": True}

    for response in draft.get("responses", []) if isinstance(draft, dict) else []:
        if isinstance(response, dict):
            response["kb_evidence_found"] = evidence_found.get(str(response.get("requirement_id", "")), context.found)

    draft["_meta"] = {
        "rfp_id": rfp_id,
        "agent_id": agent_id,
        "run_id": result.run_id,
        "violations_redacted": len(violations),
        "kb_sources": len(context.docs),
    }
    return draft


def answer_single_requirement(
    *,
    requirement: dict[str, Any],
    context: RetrievedContext,
    tone: str,
    agent_id: str,
    user_context: str = "",
    allow_web_search: bool = False,
    guidance: str = "",
) -> dict[str, Any]:
    """Process a single requirement through the agent and return structured response."""
    req_id = requirement.get("id", "?")
    req_text = requirement.get("text", "")

    context_preamble = ""
    if user_context:
        context_preamble = f"## Company / Response Context\n{fence('USER-CONTEXT', user_context)}\n\n"

    requirement_block = fence(
        "REQUIREMENT",
        f"Requirement ID: {req_id}\n"
        f"Category: {requirement.get('category', 'general')}\n"
        f"Priority: {requirement.get('priority', 'standard')}\n"
        f"Requirement: {req_text}",
    )

    run_result = runner.run_agent_prompt(
        agent_id,
        (
            f"Respond to this single RFP requirement. Tone: {tone}.\n\n"
            f"{context_preamble}"
            f"{guidance_block(guidance)}"
            f"Return ONLY a JSON object (no markdown wrapping) with these fields:\n"
            f"- \"requirement_id\": {json.dumps(str(req_id))}\n"
            f"- \"requirement_text\": the original requirement text\n"
            f"- \"answer_markdown\": your detailed response (2-4 paragraphs)\n"
            f"{_COMPLIANCE_FIELDS}\n"
            f"Ground your answer using these sources:\n\n"
            f"{context.block}\n\n"
            f"{requirement_block}"
        ),
        web_search=allow_web_search,
    )

    if not run_result.completed:
        logger.error("Requirement %s run failed: %s", req_id, run_result.status)
        return error_answer(
            requirement,
            message=f"Agent run failed with status: {run_result.status}",
            risk="Agent processing failed",
            kb_evidence_found=context.found,
        )

    safe_text, _ = runner.post_generation_guard(run_result.text)
    # Only sources retrieved for this requirement (or returned by web grounding) may be cited.
    allowed_urls = context.urls | run_result.annotation_urls

    try:
        answer = kb_search.sanitize_citations(normalize_answer(runner.parse_json_object(safe_text)), allowed_urls)
        answer["requirement_id"] = req_id
        answer["kb_evidence_found"] = context.found
        return answer
    except (ValueError, IndexError):
        return {
            "requirement_id": req_id,
            "requirement_text": req_text,
            "answer_markdown": kb_search.sanitize_citations(safe_text, allowed_urls),
            "meets": "Partial",
            "evidence": [],
            "assumptions": [],
            "risks": [],
            "followups": [],
            "kb_evidence_found": context.found,
        }


def run_single_answer(
    *,
    question: str,
    client_id: str,
    project_id: str,
    solution_area: str | None = None,
    user_oid: str,
    user_groups: list[str] | None = None,
    allow_web_search: bool = False,
) -> dict[str, Any]:
    """Answer a single question using the grounded agent."""

    # Retrieve relevant context (hybrid: keyword + vector)
    (vector,) = kb_search.embed_queries([question])
    context = RetrievedContext(
        kb_search.search_kb(
            query=question,
            vector=vector,
            client_id=client_id,
            project_id=project_id,
            solution_area=solution_area,
            user_oid=user_oid,
            user_groups=user_groups,
        )
    )

    agent_id = lifecycle.ensure_agent(lifecycle.QA_AGENT_NAME)

    result = runner.run_agent_prompt(
        agent_id,
        (
            f"Answer the following question using ONLY the retrieved sources below.  "
            f"Return a JSON object with answer_markdown, evidence, assumptions, "
            f"and followups.\n\n"
            f"{context.block}\n\n"
            f"Question:\n{fence('QUESTION', question)}"
        ),
        web_search=allow_web_search,
    )

    if not result.completed:
        return {"error": f"Agent run ended with status: {result.status}"}

    safe_text, _ = runner.post_generation_guard(result.text)
    allowed_urls = context.urls | result.annotation_urls

    try:
        answer = kb_search.sanitize_citations(
            normalize_answer(runner.parse_json_object(safe_text), require_meets=False), allowed_urls
        )
    except (ValueError, IndexError):
        answer = {"raw_response": kb_search.sanitize_citations(safe_text, allowed_urls), "parse_error": True}
    if isinstance(answer, dict):
        answer["kb_evidence_found"] = context.found
    return answer
