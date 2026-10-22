"""Grouping requirements into template sections and writing a narrative for each section."""

from __future__ import annotations

import concurrent.futures
import json
import logging
from collections.abc import Callable, Iterator
from typing import Any

from app.agents import kb_search, runner
from app.agents.kb_search import RetrievedContext
from app.agents.normalize import normalize_section
from app.agents.untrusted import fence, guidance_block

logger = logging.getLogger(__name__)

DEFAULT_SECTION_NAMES = [
    "Executive Summary",
    "Solution Overview",
    "Technical Approach",
    "Compliance & Security",
    "Implementation & Support",
]


def section_names_for(template_sections: list[dict[str, Any]] | None) -> list[str]:
    """The headings of the response template, or the default sections when there is no template."""
    return [s["heading"] for s in template_sections] if template_sections else list(DEFAULT_SECTION_NAMES)


def parse_section_map(text: str, section_names: list[str], requirement_ids: list[Any]) -> dict[str, list[str]]:
    """The model's section classification, restricted to real sections and real requirement IDs.

    Anything the model invented (extra sections, unknown IDs, repeats) is dropped; requirements it left
    out go to the first section so none disappear from the narratives. Empty if the reply is unusable.
    """
    try:
        parsed = json.loads(runner.strip_code_fence(text))
    except ValueError:
        return {}
    if not isinstance(parsed, dict):
        return {}

    known = {str(i) for i in requirement_ids}
    seen: set[str] = set()
    section_map: dict[str, list[str]] = {}
    for name in section_names:
        ids = parsed.get(name)
        if not isinstance(ids, list):
            continue
        kept = [str(i) for i in ids if str(i) in known and str(i) not in seen]
        seen.update(kept)
        if kept:
            section_map[name] = kept
    if not section_map:
        return {}
    leftover = [str(i) for i in requirement_ids if str(i) not in seen]
    if leftover:
        first = next(iter(section_map))
        section_map[first] = section_map[first] + leftover
    return section_map


def classify_sections(agent_id: str, section_names: list[str], items: list[tuple[Any, str]]) -> dict[str, list[str]]:
    """Ask the agent which section each (requirement id, text) belongs in.

    Falls back to putting everything in the first section if the reply is unusable.
    """
    summaries = fence("REQUIREMENTS", "\n".join(f"- {rid}: {text[:120]}" for rid, text in items))
    result = runner.run_agent_prompt(
        agent_id,
        (
            f"Classify each requirement below into the most appropriate document section.\n\n"
            f"Available sections: {json.dumps(section_names)}\n\n"
            f"Requirements:\n{summaries}\n\n"
            f"Return ONLY a JSON object mapping section names to arrays of requirement IDs.\n"
            f"Example: {{\"Executive Summary\": [\"F1\",\"F2\"], \"Technical Approach\": [\"F3\"]}}\n"
            f"Every requirement must appear in exactly one section."
        ),
    )
    section_map = parse_section_map(result.text, section_names, [rid for rid, _ in items]) if result.completed else {}
    return section_map or {section_names[0]: [rid for rid, _ in items]}


def _failed_section(section_name: str, req_ids: list[str], status: str) -> dict[str, Any]:
    return {
        "section_name": section_name,
        "narrative_markdown": f"Section generation failed: {status}",
        "requirement_ids": req_ids,
    }


def _parse_section(
    text: str, *, section_name: str, req_ids: list[str], allowed_urls: set[str]
) -> dict[str, Any]:
    try:
        parsed = runner.parse_json_object(text)
        return kb_search.sanitize_citations(
            normalize_section(parsed, section_name=section_name, req_ids=req_ids), allowed_urls
        )
    except (ValueError, IndexError):
        return {
            "section_name": section_name,
            "narrative_markdown": kb_search.sanitize_citations(text, allowed_urls),
            "requirement_ids": req_ids,
        }


def generate_section_narrative(
    *,
    section_name: str,
    req_ids: list[str],
    section_reqs: list[dict[str, Any]],
    context: RetrievedContext,
    tone: str,
    agent_id: str,
    guidance: str = "",
) -> dict[str, Any]:
    """Write one section narrative from the requirements it covers and the sources retrieved for them."""
    req_details = fence(
        "REQUIREMENTS",
        "\n".join(f"- [{r.get('id','?')}] ({r.get('priority','standard')}): {r.get('text','')}" for r in section_reqs),
    )

    result = runner.run_agent_prompt(
        agent_id,
        (
            f"Write the {json.dumps(section_name)} section of an RFP response document. "
            f"Tone: {tone}.\n\n"
            f"This section must address the following requirements:\n{req_details}\n\n"
            f"Write a coherent, professional narrative (not a list of individual answers). "
            f"Weave the requirements naturally into the narrative. "
            f"Reference requirement IDs in brackets where relevant, e.g. [F1].\n\n"
            f"Use these sources for grounding:\n{context.block}\n\n"
            f"{guidance_block(guidance)}"
            f"Return ONLY a JSON object:\n"
            f"{{\"section_name\": {json.dumps(section_name)}, "
            f"\"narrative_markdown\": \"<your section text>\", "
            f"\"requirement_ids\": {json.dumps(req_ids)}}}"
        ),
    )
    if not result.completed:
        return _failed_section(section_name, req_ids, result.status)

    safe, _ = runner.post_generation_guard(result.text)
    return _parse_section(
        safe, section_name=section_name, req_ids=req_ids, allowed_urls=context.urls | result.annotation_urls
    )


def generate_section_narrative_from_answers(
    *,
    section_name: str,
    req_ids: list[str],
    section_answers: list[dict[str, Any]],
    tone: str,
    agent_id: str,
    guidance: str = "",
) -> dict[str, Any]:
    """Write one section narrative by synthesizing answers that were already generated."""
    answers_digest = fence(
        "ANSWERS",
        "\n\n".join(
            f"### [{a.get('requirement_id', '?')}] (Compliance: {a.get('meets', '?')})\n"
            f"Requirement: {a.get('requirement_text', '')[:200]}\n"
            f"Response: {a.get('answer_markdown', '')}"
            for a in section_answers
        ),
    )

    result = runner.run_agent_prompt(
        agent_id,
        (
            f"Write the {json.dumps(section_name)} section of a professional RFP response document.\n"
            f"Tone: {tone}.\n\n"
            f"Below are the individual requirement responses that belong in this section. "
            f"Your job is to synthesize them into a coherent, flowing narrative — NOT a list of "
            f"individual answers. Weave them together naturally. Reference requirement IDs "
            f"in brackets where relevant, e.g. [F1].\n\n"
            f"Keep all factual claims from the original answers. Do not add new information.\n\n"
            f"--- Answers for this section ---\n\n{answers_digest}\n\n"
            f"{guidance_block(guidance)}"
            f"Return ONLY a JSON object:\n"
            f"{{\"section_name\": {json.dumps(section_name)}, "
            f"\"narrative_markdown\": \"<coherent section text>\", "
            f"\"requirement_ids\": {json.dumps(req_ids)}}}"
        ),
    )
    if not result.completed:
        return _failed_section(section_name, req_ids, result.status)

    safe, _ = runner.post_generation_guard(result.text)
    # The answers were already checked; the narrative may cite only what they cite.
    return _parse_section(
        safe,
        section_name=section_name,
        req_ids=req_ids,
        allowed_urls=kb_search.cited_urls(section_answers) | result.annotation_urls,
    )


def write_sections(
    jobs: list[dict[str, Any]], generate: Callable[..., dict[str, Any]], workers: int
) -> Iterator[dict[str, Any]]:
    """Run ``generate(**job)`` for every job, one by one or in parallel, yielding each finished section."""
    if workers <= 1 or len(jobs) <= 1:
        for job in jobs:
            logger.info("Writing section '%s'", job["section_name"])
            yield generate(**job)
        return

    logger.info("Writing %d sections in parallel (workers=%d)", len(jobs), workers)
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(generate, **job): job for job in jobs}
        for future in concurrent.futures.as_completed(futures):
            job = futures[future]
            try:
                yield future.result()
            except Exception as exc:
                logger.error("Parallel section generation failed for '%s': %s", job["section_name"], exc)
                yield _failed_section(job["section_name"], job.get("req_ids", []), str(exc))


def summaries_from_sections(section_narratives: list[dict[str, Any]], placeholder: str) -> tuple[str, str]:
    """The executive summary and solution overview, taken from the narratives that cover them."""

    def narrative_of(matches: Callable[[str], bool]) -> str:
        for section in section_narratives:
            if matches(str(section.get("section_name", "")).lower()):
                return str(section.get("narrative_markdown", ""))
        return placeholder

    return (
        narrative_of(lambda name: "executive" in name),
        narrative_of(lambda name: "overview" in name or "solution" in name),
    )
