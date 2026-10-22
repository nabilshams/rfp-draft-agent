"""The three ways of drafting a response, as generators that stream results as they are produced.

1. ``stream_per_requirement`` – each requirement answered on its own (best for compliance-heavy RFPs).
2. ``stream_per_section`` – requirements grouped into template sections, one narrative per section, plus a
   compliance matrix.
3. ``stream_map_reduce`` – answer every requirement, then synthesize the answers into section narratives.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

from app.agents import answers, kb_search, lifecycle, sections
from app.agents.kb_search import RetrievedContext
from app.config import get_settings

logger = logging.getLogger(__name__)


def _retriever(requirements, *, client_id, project_id, solution_area, user_oid, user_groups):
    return kb_search.RequirementRetriever(
        requirements,
        client_id=client_id,
        project_id=project_id,
        solution_area=solution_area,
        user_oid=user_oid,
        user_groups=user_groups,
    )


def stream_per_requirement(
    *,
    requirements: list[dict[str, Any]],
    client_id: str,
    project_id: str,
    solution_area: str | None = None,
    tone: str = "professional",
    user_oid: str,
    user_groups: list[str] | None = None,
    user_context: str = "",
    allow_web_search: bool = False,
    guidance: str = "",
) -> Iterator[dict[str, Any]]:
    """Process each requirement independently, yielding one response dict per requirement."""
    # Each requirement gets the documents that match *its* text
    retriever = _retriever(
        requirements,
        client_id=client_id,
        project_id=project_id,
        solution_area=solution_area,
        user_oid=user_oid,
        user_groups=user_groups,
    )

    agent_id = lifecycle.ensure_agent(lifecycle.DRAFT_AGENT_NAME)
    logger.info("Streaming draft: %d requirements, agent=%s", len(requirements), agent_id)

    for i, req in enumerate(requirements):
        logger.info("Processing requirement %d/%d: %s", i + 1, len(requirements), req.get("id", "?"))
        try:
            yield answers.answer_single_requirement(
                requirement=req,
                context=lambda i=i: retriever.context(i),  # retrieved inside the requirement's span
                tone=tone,
                agent_id=agent_id,
                user_context=user_context,
                allow_web_search=allow_web_search,
                guidance=guidance,
            )
        except Exception as exc:
            logger.error("Error processing requirement %s: %s", req.get("id", "?"), exc)
            yield answers.exception_answer(req, exc)


def stream_per_section(
    *,
    requirements: list[dict[str, Any]],
    template_sections: list[dict[str, Any]],
    client_id: str,
    project_id: str,
    solution_area: str | None = None,
    tone: str = "professional",
    user_oid: str,
    user_groups: list[str] | None = None,
    user_context: str = "",
    allow_web_search: bool = False,
    guidance: str = "",
) -> Iterator[dict[str, Any]]:
    """Group requirements by template section and write a narrative per section.

    Step 1 — classify requirements into template sections.
    Step 2 — write a coherent narrative per section.
    Step 3 — produce compliance matrix entries for all requirements.
    Yields events: section_map, section_narrative, requirement_started, requirement_compliance.
    """
    settings = get_settings()
    agent_id = lifecycle.ensure_agent(lifecycle.DRAFT_AGENT_NAME)

    # Each section's narrative draws on the documents retrieved for its own requirements
    contexts = kb_search.retrieve_for_requirements(
        requirements,
        client_id=client_id,
        project_id=project_id,
        solution_area=solution_area,
        user_oid=user_oid,
        user_groups=user_groups,
    )
    index_by_id = {r.get("id", "?"): i for i, r in enumerate(requirements)}
    req_by_id = {r.get("id", "?"): r for r in requirements}

    def section_context(req_ids: list[str]) -> RetrievedContext:
        return kb_search.merge_contexts([contexts[index_by_id[rid]] for rid in req_ids if rid in index_by_id])

    section_names = sections.section_names_for(template_sections)

    # Step 1
    logger.info("Step 1: Classifying %d requirements into %d sections", len(requirements), len(section_names))
    section_map = sections.classify_sections(
        agent_id, section_names, [(r.get("id", "?"), r.get("text", "")) for r in requirements]
    )
    yield {"type": "section_map", "data": section_map}
    logger.info("Section mapping: %s", {k: len(v) for k, v in section_map.items()})

    # Step 2
    jobs = []
    for section_name, req_ids in section_map.items():
        section_reqs = [req_by_id[rid] for rid in req_ids if rid in req_by_id]
        if section_reqs:
            jobs.append(
                {
                    "section_name": section_name,
                    "req_ids": req_ids,
                    "section_reqs": section_reqs,
                    "context": section_context(req_ids),
                    "tone": tone,
                    "agent_id": agent_id,
                    "guidance": guidance,
                }
            )
    for section_data in sections.write_sections(
        jobs, sections.generate_section_narrative, max(1, settings.section_parallelism)
    ):
        yield {"type": "section_narrative", "data": section_data}

    # Step 3
    logger.info("Generating compliance matrix for %d requirements", len(requirements))
    for idx, req in enumerate(requirements, start=1):
        yield {
            "type": "requirement_started",
            "data": {"requirement_id": req.get("id", "?"), "index": idx, "total": len(requirements)},
        }
        try:
            result = answers.answer_single_requirement(
                requirement=req,
                context=contexts[idx - 1],
                tone=tone,
                agent_id=agent_id,
                user_context=user_context,
                allow_web_search=allow_web_search,
                guidance=guidance,
            )
            # Only keep the answer's own fields, not the whole result
            data = {
                "requirement_id": result.get("requirement_id", req.get("id", "?")),
                "meets": result.get("meets", "No"),
                "answer_markdown": result.get("answer_markdown", ""),
                "evidence": result.get("evidence", []),
                **{
                    key: result[key]
                    for key in ("assumptions", "risks", "followups", "kb_evidence_found", "generation_failed")
                    if key in result
                },
            }
        except Exception as exc:
            data = {
                "requirement_id": req.get("id", "?"),
                "meets": "No",
                "answer_markdown": f"Error: {exc}",
                "evidence": [],
                "generation_failed": True,
            }
        yield {"type": "requirement_compliance", "data": data}


def stream_map_reduce(
    *,
    requirements: list[dict[str, Any]],
    template_sections: list[dict[str, Any]],
    client_id: str,
    project_id: str,
    solution_area: str | None = None,
    tone: str = "professional",
    user_oid: str,
    user_groups: list[str] | None = None,
    user_context: str = "",
    allow_web_search: bool = False,
    guidance: str = "",
) -> Iterator[dict[str, Any]]:
    """Answer each requirement, then synthesize the answers into section narratives.

    Phase 1 (Map): process each requirement individually → 'response' events.
    Phase 2 (Reduce): group the answers by template section and write narratives → 'section_narrative' events.

    This separates accuracy (Phase 1) from presentation (Phase 2).
    """
    settings = get_settings()
    agent_id = lifecycle.ensure_agent(lifecycle.DRAFT_AGENT_NAME)

    # Retrieval happens on demand, in Phase 1
    retriever = _retriever(
        requirements,
        client_id=client_id,
        project_id=project_id,
        solution_area=solution_area,
        user_oid=user_oid,
        user_groups=user_groups,
    )
    section_names = sections.section_names_for(template_sections)

    # ── Phase 1: Map — answer every requirement individually ──
    logger.info("Phase 1: Answering %d requirements individually", len(requirements))
    yield {"type": "phase", "data": {"phase": 1, "description": "Answering each requirement individually"}}

    all_responses: list[dict[str, Any]] = []
    for i, req in enumerate(requirements):
        logger.info("Phase 1: Requirement %d/%d: %s", i + 1, len(requirements), req.get("id", "?"))
        try:
            result = answers.answer_single_requirement(
                requirement=req,
                context=lambda i=i: retriever.context(i),  # retrieved inside the requirement's span
                tone=tone,
                agent_id=agent_id,
                user_context=user_context,
                allow_web_search=allow_web_search,
                guidance=guidance,
            )
        except Exception as exc:
            logger.error("Error on requirement %s: %s", req.get("id", "?"), exc)
            result = answers.exception_answer(req, exc)
        all_responses.append(result)
        yield {"type": "response", "data": result}

    # ── Phase 2: Reduce — classify and synthesize into sections ──
    logger.info("Phase 2: Synthesizing %d answers into %d sections", len(all_responses), len(section_names))
    yield {"type": "phase", "data": {"phase": 2, "description": "Synthesizing answers into template sections"}}

    section_map = sections.classify_sections(
        agent_id,
        section_names,
        [(r.get("requirement_id", "?"), r.get("requirement_text", r.get("text", ""))) for r in all_responses],
    )
    yield {"type": "section_map", "data": section_map}

    resp_by_id = {r.get("requirement_id", ""): r for r in all_responses}
    jobs = []
    for section_name, req_ids in section_map.items():
        section_answers = [resp_by_id[rid] for rid in req_ids if rid in resp_by_id]
        if section_answers:
            jobs.append(
                {
                    "section_name": section_name,
                    "req_ids": req_ids,
                    "section_answers": section_answers,
                    "tone": tone,
                    "agent_id": agent_id,
                    "guidance": guidance,
                }
            )
    for section_data in sections.write_sections(
        jobs, sections.generate_section_narrative_from_answers, max(1, settings.section_parallelism)
    ):
        yield {"type": "section_narrative", "data": section_data}
