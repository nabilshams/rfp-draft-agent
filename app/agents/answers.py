"""Generating answers: a full draft in one run, one requirement at a time, or a single free-form question."""

from __future__ import annotations

import json
import logging
from typing import Any

from app.agents import confidence, grounding, kb_search, lifecycle, runner, states, verify
from app.agents.kb_search import RetrievedContext
from app.agents.normalize import normalize_answer, normalize_draft
from app.agents.untrusted import fence, guidance_block
from app.config import get_settings
from app.telemetry import spans

logger = logging.getLogger(__name__)

_COMPLIANCE_FIELDS = (
    '- "meets": "Yes", "Partial", or "No"\n'
    '- "sufficiency": "complete", "partial", "none", "needs_sme" or "conflict" (how well the sources cover the '
    'requirement; with "none" or "needs_sme" leave answer_markdown empty and evidence empty)\n'
    '- "missing": what the sources do not cover, in one short phrase (empty when complete)\n'
    '- "evidence": array of {"id" ("E1", "E2"...), "title", "source_url", "quote" (verbatim from the source)}\n'
    '- "claims": array of {"text", "evidence_ids"}: each factual statement the answer makes (a capability, number, '
    'certification, commitment or name) as one short sentence, with the ids of the evidence entries that support it\n'
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
    """The record of a requirement whose generation failed: state ``failed`` and no compliance claim (never "No")."""
    return states.failed_answer(requirement, message=message, risk=risk, kb_evidence_found=kb_evidence_found)


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
        f"  - \"sufficiency\": \"complete\", \"partial\", \"none\", \"needs_sme\" or \"conflict\" (how well the sources "
        f"cover it; with \"none\" or \"needs_sme\" leave answer_markdown and evidence empty)\n"
        f"  - \"missing\": what the sources do not cover, in one short phrase\n"
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
    # Figures must come from what the model was given (not checkable for web-grounded text, so trusted then)
    given = "\n".join([context.text, requirements_json, guidance])
    if allow_web_search:
        given += "\n" + safe_text

    try:
        draft = kb_search.enrich_evidence(
            kb_search.sanitize_citations(normalize_draft(runner.parse_json_object(safe_text)), allowed_urls),
            context.docs,
        )
    except (ValueError, IndexError):
        draft = {"raw_response": kb_search.sanitize_citations(safe_text, allowed_urls), "parse_error": True}

    router = states.router_from_settings(get_settings())
    by_id = {str(r.get("id", "")): r for r in requirements}
    if isinstance(draft, dict) and isinstance(draft.get("responses"), list):
        draft["responses"] = [
            _settled(response, by_id, router, evidence_found, context, bool(result.annotation_urls), given)
            for response in draft["responses"]
            if isinstance(response, dict)
        ]
    if isinstance(draft, dict):
        grounding.ground_fields(draft, ("executive_summary", "solution_overview"), given)

    draft["_meta"] = {
        "rfp_id": rfp_id,
        "agent_id": agent_id,
        "run_id": result.run_id,
        "violations_redacted": len(violations),
        "kb_sources": len(context.docs),
    }
    return draft


def _settled(
    response: dict[str, Any],
    by_id: dict[str, dict[str, Any]],
    router: states.Router,
    evidence_found: dict[str, bool],
    context: RetrievedContext,
    web_grounded: bool,
    given: str,
) -> dict[str, Any]:
    """One response of a full draft with its state decided (see :mod:`app.agents.states`)."""
    rid = str(response.get("requirement_id", ""))
    requirement = by_id.get(rid, {"id": rid, "text": response.get("requirement_text", "")})
    found = evidence_found.get(rid, context.found)
    routed = router.route(requirement)
    if routed:
        return states.placeholder_answer(requirement, routed, kb_evidence_found=found)
    response["kb_evidence_found"] = found
    grounding.note_removed(response, grounding.ground_fields(response, ("answer_markdown",), given))
    return states.finalize(response, requirement, docs=context.docs, web_grounded=web_grounded)


def answer_single_requirement(**kwargs: Any) -> dict[str, Any]:
    """Process a single requirement through the agent and return structured response.

    Recorded as a ``requirement`` span with the stages beneath it: retrieval (when ``context`` is given as a function,
    so that it runs inside the span), the model call and the figure check. Other arguments are those of
    :func:`_answer_single_requirement`.
    """
    requirement = kwargs["requirement"]
    with spans.stage("requirement", requirement__id=str(requirement.get("id", "?"))[:64]) as span:
        context = kwargs["context"]
        if callable(context):
            context = context()
        span.set(context__docs=len(context.docs))
        answer = confidence.attach(_answer_single_requirement(**{**kwargs, "context": context}), context.docs)
        span.set(
            evidence=len(answer.get("evidence") or []),
            citations=len(kb_search.cited_urls(answer)),
            answer__chars=len(str(answer.get("answer_markdown", ""))),
            answer__state=str(answer.get("state", "")),
            verification__status=str((answer.get("verification") or {}).get("status", "")),
            confidence__band=str(answer["confidence"]["band"]),
            outcome="ok",
        )
        return answer


def _answer_single_requirement(
    *,
    requirement: dict[str, Any],
    context: RetrievedContext,
    tone: str,
    agent_id: str,
    user_context: str = "",
    allow_web_search: bool = False,
    guidance: str = "",
) -> dict[str, Any]:
    """The work of :func:`answer_single_requirement`."""
    req_id = requirement.get("id", "?")
    req_text = requirement.get("text", "")

    # Some requirements are never drafted (pricing, contract terms), and with nothing retrieved there is nothing to
    # draft from: say so without asking the model.
    gated = states.pre_gate(
        requirement,
        router=states.router_from_settings(get_settings()),
        evidence_found=context.found,
        web_search=allow_web_search,
    )
    if gated is not None:
        logger.info("Requirement %s is %s: not sent to the model", req_id, gated["state"])
        return gated

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
    # Everything the model was given. Figures anywhere else are inventions, except that web-grounded text can't be
    # checked here (its pages aren't visible to us), so with web search on the answer is trusted for figures.
    given = "\n".join([context.text, req_text, user_context, guidance])
    if allow_web_search:
        given += "\n" + safe_text

    try:
        answer = kb_search.sanitize_citations(normalize_answer(runner.parse_json_object(safe_text)), allowed_urls)
        answer = kb_search.enrich_evidence(answer, context.docs)
        answer["requirement_id"] = req_id
        answer["kb_evidence_found"] = context.found
        grounding.note_removed(answer, grounding.ground_fields(answer, ("answer_markdown",), given))
        web_grounded = bool(run_result.annotation_urls)
        settled = states.finalize(answer, requirement, docs=context.docs, web_grounded=web_grounded)
        return verify.verify_answer(settled, requirement, docs=context.docs, web_grounded=web_grounded)
    except (ValueError, IndexError):
        # The reply was not the JSON we asked for. Its text is not kept as an answer: it is a failed generation.
        logger.warning("Requirement %s: the model did not return a usable answer object", req_id)
        return error_answer(
            requirement,
            message="The model did not return a usable answer.",
            risk="The model reply could not be read",
            kb_evidence_found=context.found,
        )


def _free_form(answer: dict[str, Any]) -> dict[str, Any]:
    """An answer to a free-form question has no requirement and no compliance claim: drop what a requirement carries."""
    return {k: v for k, v in answer.items() if k not in ("requirement_id", "requirement_text", "meets")}


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

    # The same rules as a requirement: pricing and contract questions go to people, and with nothing retrieved
    # there is nothing to answer from
    question_as_requirement = {"id": "", "text": question}
    gated = states.pre_gate(
        question_as_requirement,
        router=states.router_from_settings(get_settings()),
        evidence_found=context.found,
        web_search=allow_web_search,
    )
    if gated is not None:
        return {**_free_form(gated), "kb_evidence_found": context.found}

    agent_id = lifecycle.ensure_agent(lifecycle.QA_AGENT_NAME)

    result = runner.run_agent_prompt(
        agent_id,
        (
            f"Answer the following question using ONLY the retrieved sources below.  "
            f"Return a JSON object with answer_markdown, evidence, assumptions, followups, "
            f"sufficiency (complete, partial, none, needs_sme or conflict) and missing.\n\n"
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
    if isinstance(answer, dict) and not answer.get("parse_error"):
        answer["kb_evidence_found"] = context.found
        answer = _free_form(
            states.finalize(answer, question_as_requirement, docs=context.docs, web_grounded=bool(result.annotation_urls))
        )
    elif isinstance(answer, dict):
        answer["kb_evidence_found"] = context.found
    return answer
