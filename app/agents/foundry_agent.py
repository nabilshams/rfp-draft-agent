"""Azure AI Foundry Agent Service – code-first agent with retrieval-augmented generation.

Instead of using the built-in AzureAISearchTool (which requires Enterprise
Agent setup), this module performs search retrieval directly via the Azure
AI Search SDK, then passes the retrieved context into the agent message.
This gives full control over DLAC filters and works with Basic agent setup.

Agents are **persistent singletons** – created once on first use and reused
for all subsequent requests.  If instructions change, the agent is updated
in place.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from azure.ai.agents import AgentsClient
from azure.ai.agents.models import BingGroundingTool, MessageRole
from azure.identity import DefaultAzureCredential

from app.agents import kb_search
from app.agents.kb_search import RetrievedContext
from app.agents.normalize import normalize_answer, normalize_draft, normalize_section
from app.agents.prompts import AGENT_INSTRUCTIONS
from app.agents.untrusted import fence
from app.config import get_settings
from app.ingestion.redaction import get_redaction_engine

logger = logging.getLogger(__name__)

# ── Singleton state ──────────────────────────────────────
_agents_client: AgentsClient | None = None
_draft_agent_id: str | None = None
_qa_agent_id: str | None = None
_current_instructions_hash: str | None = None

_DRAFT_AGENT_NAME = "rfp-draft-agent"
_QA_AGENT_NAME = "rfp-qa-agent"


class WebSearchNotConfigured(ValueError):
    """Web grounding was requested but no Grounding-with-Bing connection is configured."""


def web_search_available() -> bool:
    return bool(get_settings().bing_connection_id)


def _web_search_tools() -> list[Any]:
    connection_id = get_settings().bing_connection_id
    if not connection_id:
        raise WebSearchNotConfigured("Web search is not configured: set BING_CONNECTION_ID.")
    return BingGroundingTool(connection_id=connection_id).definitions


def _get_agents_client() -> AgentsClient:
    global _agents_client
    if _agents_client is None:
        settings = get_settings()
        base_endpoint = settings.foundry_project_endpoint.rstrip("/")
        # Remove /openai suffix if present (legacy)
        if base_endpoint.endswith("/openai"):
            base_endpoint = base_endpoint[:-7]
        # Use the project-scoped endpoint so agents are visible in AI Foundry portal
        project_name = settings.foundry_project_name
        if project_name:
            endpoint = f"{base_endpoint}/api/projects/{project_name}"
        else:
            endpoint = base_endpoint
        logger.info("AgentsClient endpoint: %s", endpoint)
        _agents_client = AgentsClient(
            endpoint=endpoint,
            credential=DefaultAzureCredential(),
        )
    return _agents_client


def _instructions_hash() -> str:
    """Hash of current instructions to detect changes."""
    import hashlib
    return hashlib.md5(AGENT_INSTRUCTIONS.encode()).hexdigest()[:12]


def _find_existing_agent(client: AgentsClient, name: str) -> str | None:
    """Search for an existing agent by name. Returns agent ID or None."""
    try:
        for agent in client.list_agents():
            if agent.name == name:
                logger.info("Found existing agent '%s' → %s", name, agent.id)
                return agent.id
    except Exception as exc:
        logger.warning("Could not list agents to find '%s': %s", name, exc)
    return None


def _has_web_tool(agent: Any) -> bool:
    for tool in getattr(agent, "tools", None) or []:
        kind = tool.get("type") if isinstance(tool, dict) else getattr(tool, "type", "")
        if str(kind).startswith("bing"):
            return True
    return False


def _ensure_no_standing_web_tool(client: AgentsClient, agent_id: str, name: str) -> None:
    """Web grounding is opt-in per request, so no agent may carry it permanently.

    Agents created by earlier versions had Bing attached; remove it and confirm, because a Bing tool left
    in place would send query text outside the Azure compliance boundary on every run.
    """
    if not _has_web_tool(client.get_agent(agent_id)):
        return
    logger.warning("Agent '%s' has a standing Bing tool; removing it (web search is now per-request)", name)
    client.update_agent(agent_id=agent_id, tools=[])
    if _has_web_tool(client.get_agent(agent_id)):
        raise RuntimeError(f"Agent '{name}' still has a Bing grounding tool attached; remove it in the Foundry portal.")


def _ensure_agent(name: str) -> str:
    """Get or create a persistent agent by name.

    If instructions have changed since the agent was last created/updated,
    the agent is updated in place. If creation fails, raises with a clear error.
    Agents carry no tools: web grounding is attached to individual runs that ask for it.

    Returns the agent ID.
    """
    global _draft_agent_id, _qa_agent_id, _current_instructions_hash

    client = _get_agents_client()
    settings = get_settings()
    new_hash = _instructions_hash()

    # Pick the right cached ID
    if name == _DRAFT_AGENT_NAME:
        cached_id = _draft_agent_id
    else:
        cached_id = _qa_agent_id

    # If we have a cached ID and instructions haven't changed, verify it still exists
    if cached_id and _current_instructions_hash == new_hash:
        try:
            agent = client.get_agent(cached_id)
            if agent and agent.id:
                return cached_id
        except Exception:
            logger.info("Cached agent %s no longer exists, will recreate", cached_id)
            cached_id = None

    # Try to find an existing agent with this name
    existing_id = _find_existing_agent(client, name)

    if existing_id:
        # Update instructions if they changed
        if _current_instructions_hash != new_hash:
            try:
                client.update_agent(
                    agent_id=existing_id,
                    model=settings.foundry_model_deployment_name,
                    name=name,
                    instructions=AGENT_INSTRUCTIONS,
                )
                logger.info("Updated agent '%s' (%s) with new instructions", name, existing_id)
            except Exception as exc:
                logger.warning("Could not update agent '%s': %s", name, exc)

        _ensure_no_standing_web_tool(client, existing_id, name)
        agent_id = existing_id
    else:
        logger.info("No existing agent '%s' found, creating new one...", name)
        try:
            agent = client.create_agent(
                model=settings.foundry_model_deployment_name,
                name=name,
                instructions=AGENT_INSTRUCTIONS,
            )
        except Exception as exc:
            logger.error("Failed to create agent '%s': %s", name, exc)
            raise RuntimeError(f"Cannot create or find agent '{name}': {exc}") from exc
        agent_id = agent.id
        logger.info("Created persistent agent '%s' → %s", name, agent_id)

    # Cache the ID
    if name == _DRAFT_AGENT_NAME:
        _draft_agent_id = agent_id
    else:
        _qa_agent_id = agent_id
    _current_instructions_hash = new_hash

    return agent_id


def setup_agents() -> dict[str, str]:
    """Pre-create (or verify) all persistent agents at startup.

    Returns a dict mapping agent name → agent ID.
    """
    logger.info("Setting up persistent agents...")
    draft_id = _ensure_agent(_DRAFT_AGENT_NAME)
    qa_id = _ensure_agent(_QA_AGENT_NAME)
    logger.info(
        "Agents ready: draft=%s, qa=%s", draft_id, qa_id
    )
    return {_DRAFT_AGENT_NAME: draft_id, _QA_AGENT_NAME: qa_id}


def _post_generation_guard(text: str) -> tuple[str, list[dict]]:
    """Run the redaction engine's output scanner.  Returns (safe_text, violations).

    Violations are redacted and the response is still returned.
    TODO: hand output that trips the scanner to a reviewing agent that can rewrite or withhold it,
    instead of only masking the matched spans.
    """
    engine = get_redaction_engine()
    violations = engine.scan_output(text)
    if violations:
        logger.warning("Post-generation guard caught %d violations", len(violations))
        # Re-redact the output
        result = engine.redact(text)
        return result.sanitized_text, violations
    return text, []


# ── Agent run helper ──────────────────────────────────────────

_ACTIVE_RUN_STATES = ("queued", "in_progress", "requires_action")


@dataclass
class AgentRunResult:
    """Outcome of one prompt sent to an agent on a throw-away thread."""

    status: str
    run_id: str = ""
    text: str = ""
    annotation_urls: set[str] = field(default_factory=set)
    has_response: bool = False
    last_error: str = ""

    @property
    def completed(self) -> bool:
        return self.status == "completed"


def _delete_thread(client: AgentsClient, thread_id: str) -> None:
    """Best-effort removal so request content doesn't accumulate in the Foundry project."""
    try:
        client.threads.delete(thread_id)
    except Exception as exc:
        logger.warning("Could not delete agent thread %s: %s", thread_id, exc)


def run_agent_prompt(agent_id: str, content: str, *, web_search: bool = False) -> AgentRunResult:
    """Send *content* to an agent on a new thread, wait for the run, and return its reply.

    Blocking: call from a worker thread, not from the event loop. The run is cancelled if
    it exceeds ``RUN_TIMEOUT_SECONDS`` (status ``timed_out``), and the thread is always deleted.
    ``web_search`` attaches Bing grounding to this run only; it raises WebSearchNotConfigured if unavailable.
    """
    run_options: dict[str, Any] = {"tools": _web_search_tools()} if web_search else {}
    client = _get_agents_client()
    timeout = get_settings().run_timeout_seconds
    thread = client.threads.create()
    try:
        client.messages.create(thread_id=thread.id, role=MessageRole.USER, content=content)
        run = client.runs.create(thread_id=thread.id, agent_id=agent_id, **run_options)
        logger.info("Started run %s", run.id)

        deadline = time.monotonic() + timeout
        status = run.status
        while status in _ACTIVE_RUN_STATES:
            if time.monotonic() >= deadline:
                logger.error("Run %s still '%s' after %ds – cancelling", run.id, status, timeout)
                try:
                    client.runs.cancel(thread_id=thread.id, run_id=run.id)
                except Exception as exc:
                    logger.warning("Could not cancel run %s: %s", run.id, exc)
                status = "timed_out"
                break
            time.sleep(1)
            run = client.runs.get(thread_id=thread.id, run_id=run.id)
            status = run.status

        result = AgentRunResult(
            status=status,
            run_id=run.id,
            last_error=str(getattr(run, "last_error", "") or ""),
        )
        if status != "completed":
            return result

        for msg in client.messages.list(thread_id=thread.id):
            if msg.role != MessageRole.AGENT:
                continue
            result.has_response = True
            for block in msg.content:
                if hasattr(block, "text"):
                    result.text += block.text.value
                    for ann in getattr(block.text, "annotations", None) or []:
                        # Web citations carry their URL under url_citation; plain annotations may have it directly.
                        url = getattr(getattr(ann, "url_citation", None), "url", None) or getattr(ann, "url", None)
                        if url:
                            result.annotation_urls.add(url)
        return result
    finally:
        _delete_thread(client, thread.id)


# ── Public API ────────────────────────────────────────────────


def _strip_code_fence(text: str) -> str:
    """The JSON inside a ```json fence if the model added one, else the text itself."""
    if "```json" in text:
        text = text.split("```json", 1)[1].split("```", 1)[0]
    elif "```" in text:
        text = text.split("```", 1)[1].split("```", 1)[0]
    return text.strip()


def _parse_section_map(text: str, section_names: list[str], requirement_ids: list[Any]) -> dict[str, list[str]]:
    """The model's section classification, restricted to real sections and real requirement IDs.

    Anything the model invented (extra sections, unknown IDs, repeats) is dropped; requirements it left
    out go to the first section so none disappear from the narratives. Empty if the reply is unusable.
    """
    try:
        parsed = json.loads(_strip_code_fence(text))
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


def _parse_requirements(requirements_json: str) -> list[dict[str, Any]]:
    """The requirement dicts in a JSON array, or an empty list if it isn't one."""
    try:
        parsed = json.loads(requirements_json)
    except json.JSONDecodeError:
        return []
    return [r for r in parsed if isinstance(r, dict)] if isinstance(parsed, list) else []


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
) -> dict[str, Any]:
    """Create an agent, submit the full RFP, and return the draft JSON."""

    # Step 1: Retrieve knowledge-base content per requirement, then share the best of it across the draft
    requirements = _parse_requirements(requirements_json) or [{"text": requirements_json[:1000]}]
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

    # Step 2: Get or create the persistent draft agent
    agent_id = _ensure_agent(_DRAFT_AGENT_NAME)
    logger.info("Using draft agent %s for RFP %s", agent_id, rfp_id)

    # Submit the full RFP to the agent
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
        f"Here are the requirements to respond to:\n{fence('REQUIREMENTS', requirements_json)}"
    )

    result = run_agent_prompt(agent_id, user_message, web_search=allow_web_search)

    if not result.completed:
        logger.error("Agent run failed: %s – %s", result.status, result.last_error)
        return {"error": f"Agent run ended with status: {result.status}"}

    if not result.has_response:
        return {"error": "No assistant response received"}

    # Post-generation guardrail
    safe_text, violations = _post_generation_guard(result.text)

    if violations:
        logger.warning("Draft contained %d sensitive patterns – redacted", len(violations))

    # Only sources that were retrieved for this draft (or returned by web grounding) may be cited.
    allowed_urls = context.urls | result.annotation_urls

    try:
        parsed = json.loads(_strip_code_fence(safe_text))
        if not isinstance(parsed, dict):
            raise ValueError("expected a JSON object")
        draft = kb_search.sanitize_citations(normalize_draft(parsed), allowed_urls)
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


def _answer_single_requirement(
    *,
    requirement: dict[str, Any],
    context: RetrievedContext,
    tone: str,
    agent_id: str,
    user_context: str = "",
    allow_web_search: bool = False,
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

    run_result = run_agent_prompt(
        agent_id,
        (
            f"Respond to this single RFP requirement. Tone: {tone}.\n\n"
            f"{context_preamble}"
            f"Return ONLY a JSON object (no markdown wrapping) with these fields:\n"
            f"- \"requirement_id\": {json.dumps(str(req_id))}\n"
            f"- \"requirement_text\": the original requirement text\n"
            f"- \"answer_markdown\": your detailed response (2-4 paragraphs)\n"
            f"- \"meets\": \"Yes\", \"Partial\", or \"No\"\n"
            f"- \"evidence\": array of {{\"title\", \"source_url\", \"quote\"}}\n"
            f"- \"assumptions\": array of strings\n"
            f"- \"risks\": array of strings\n"
            f"- \"followups\": array of strings\n\n"
            f"Ground your answer using these sources:\n\n"
            f"{context.block}\n\n"
            f"{requirement_block}"
        ),
        web_search=allow_web_search,
    )

    if not run_result.completed:
        logger.error("Requirement %s run failed: %s", req_id, run_result.status)
        return {
            "requirement_id": req_id,
            "requirement_text": req_text,
            "answer_markdown": f"Agent run failed with status: {run_result.status}",
            "meets": "No",
            "evidence": [],
            "assumptions": [],
            "risks": ["Agent processing failed"],
            "followups": [],
            "kb_evidence_found": context.found,
        }

    safe_text, _ = _post_generation_guard(run_result.text)
    # Only sources retrieved for this requirement (or returned by web grounding) may be cited.
    allowed_urls = context.urls | run_result.annotation_urls

    try:
        parsed = json.loads(_strip_code_fence(safe_text))
        if not isinstance(parsed, dict):
            raise ValueError("expected a JSON object")
        answer = kb_search.sanitize_citations(normalize_answer(parsed), allowed_urls)
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


def _generate_section_narrative(
    *,
    section_name: str,
    req_ids: list[str],
    section_reqs: list[dict[str, Any]],
    context: RetrievedContext,
    tone: str,
    agent_id: str,
) -> dict[str, Any]:
    """Generate one section narrative from a list of requirements."""

    req_details = fence(
        "REQUIREMENTS",
        "\n".join(
            f"- [{r.get('id','?')}] ({r.get('priority','standard')}): {r.get('text','')}" for r in section_reqs
        ),
    )

    result = run_agent_prompt(
        agent_id,
        (
            f"Write the {json.dumps(section_name)} section of an RFP response document. "
            f"Tone: {tone}.\n\n"
            f"This section must address the following requirements:\n{req_details}\n\n"
            f"Write a coherent, professional narrative (not a list of individual answers). "
            f"Weave the requirements naturally into the narrative. "
            f"Reference requirement IDs in brackets where relevant, e.g. [F1].\n\n"
            f"Use these sources for grounding:\n{context.block}\n\n"
            f"Return ONLY a JSON object:\n"
            f"{{\"section_name\": {json.dumps(section_name)}, "
            f"\"narrative_markdown\": \"<your section text>\", "
            f"\"requirement_ids\": {json.dumps(req_ids)}}}"
        ),
    )

    if not result.completed:
        return {
            "section_name": section_name,
            "narrative_markdown": f"Section generation failed: {result.status}",
            "requirement_ids": req_ids,
        }

    safe, _ = _post_generation_guard(result.text)
    allowed_urls = context.urls | result.annotation_urls
    try:
        parsed = json.loads(_strip_code_fence(safe))
        if not isinstance(parsed, dict):
            raise ValueError("expected a JSON object")
        section_data = kb_search.sanitize_citations(
            normalize_section(parsed, section_name=section_name, req_ids=req_ids), allowed_urls
        )
    except (ValueError, IndexError):
        section_data = {
            "section_name": section_name,
            "narrative_markdown": kb_search.sanitize_citations(safe, allowed_urls),
            "requirement_ids": req_ids,
        }
    return section_data


def _generate_section_narrative_from_answers(
    *,
    section_name: str,
    req_ids: list[str],
    section_answers: list[dict[str, Any]],
    tone: str,
    agent_id: str,
) -> dict[str, Any]:
    """Generate one section narrative from pre-generated requirement answers."""

    answers_digest = fence(
        "ANSWERS",
        "\n\n".join(
            f"### [{a.get('requirement_id', '?')}] (Compliance: {a.get('meets', '?')})\n"
            f"Requirement: {a.get('requirement_text', '')[:200]}\n"
            f"Response: {a.get('answer_markdown', '')}"
            for a in section_answers
        ),
    )

    result = run_agent_prompt(
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
            f"Return ONLY a JSON object:\n"
            f"{{\"section_name\": {json.dumps(section_name)}, "
            f"\"narrative_markdown\": \"<coherent section text>\", "
            f"\"requirement_ids\": {json.dumps(req_ids)}}}"
        ),
    )

    if not result.completed:
        return {
            "section_name": section_name,
            "narrative_markdown": f"Section generation failed: {result.status}",
            "requirement_ids": req_ids,
        }

    safe, _ = _post_generation_guard(result.text)
    # The answers were already checked; the narrative may cite only what they cite.
    allowed_urls = kb_search.cited_urls(section_answers) | result.annotation_urls
    try:
        parsed = json.loads(_strip_code_fence(safe))
        if not isinstance(parsed, dict):
            raise ValueError("expected a JSON object")
        section_data = kb_search.sanitize_citations(
            normalize_section(parsed, section_name=section_name, req_ids=req_ids), allowed_urls
        )
    except (ValueError, IndexError):
        section_data = {
            "section_name": section_name,
            "narrative_markdown": kb_search.sanitize_citations(safe, allowed_urls),
            "requirement_ids": req_ids,
        }
    return section_data

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
):
    """Approach 1: Process each requirement independently.

    Yields one response dict per requirement with requirement_id, answer, meets, etc.
    Good for: compliance-heavy RFPs where each requirement needs individual assessment.
    """
    # Retrieve per requirement: each one gets the documents that match *its* text
    retriever = kb_search.RequirementRetriever(
        requirements,
        client_id=client_id,
        project_id=project_id,
        solution_area=solution_area,
        user_oid=user_oid,
        user_groups=user_groups,
    )

    agent_id = _ensure_agent(_DRAFT_AGENT_NAME)
    logger.info("Streaming draft: %d requirements, agent=%s", len(requirements), agent_id)

    for i, req in enumerate(requirements):
        logger.info("Processing requirement %d/%d: %s", i + 1, len(requirements), req.get("id", "?"))
        try:
            result = _answer_single_requirement(
                requirement=req,
                context=retriever.context(i),
                tone=tone,
                agent_id=agent_id,
                user_context=user_context,
                allow_web_search=allow_web_search,
            )
            yield result
        except Exception as exc:
            logger.error("Error processing requirement %s: %s", req.get("id", "?"), exc)
            yield {
                "requirement_id": req.get("id", "?"),
                "requirement_text": req.get("text", ""),
                "answer_markdown": f"Error: {exc}",
                "meets": "No",
                "evidence": [],
                "assumptions": [],
                "risks": [str(exc)],
                "followups": [],
                "kb_evidence_found": False,
            }


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
):
    """Approach 2: Group requirements by template section, generate narratives.

    Step 1 — Classify requirements into template sections.
    Step 2 — Generate a coherent narrative per section.
    Step 3 — Generate compliance matrix entries for all requirements.
    Yields events: section_map, section_narrative, requirement_compliance, complete.
    """
    settings = get_settings()
    agent_id = _ensure_agent(_DRAFT_AGENT_NAME)
    section_workers = max(1, settings.section_parallelism)

    # Retrieve per requirement; each section's narrative then draws on its own requirements' documents
    contexts = kb_search.retrieve_for_requirements(
        requirements,
        client_id=client_id,
        project_id=project_id,
        solution_area=solution_area,
        user_oid=user_oid,
        user_groups=user_groups,
    )
    index_by_id = {r.get("id", "?"): i for i, r in enumerate(requirements)}

    def section_context(req_ids: list[str]) -> RetrievedContext:
        return kb_search.merge_contexts([contexts[index_by_id[rid]] for rid in req_ids if rid in index_by_id])

    section_names = [s["heading"] for s in template_sections] if template_sections else [
        "Executive Summary", "Solution Overview", "Technical Approach",
        "Compliance & Security", "Implementation & Support",
    ]

    # Step 1: Classify requirements into sections
    logger.info("Step 1: Classifying %d requirements into %d sections", len(requirements), len(section_names))
    req_summaries = fence(
        "REQUIREMENTS", "\n".join(f"- {r.get('id','?')}: {r.get('text','')[:120]}" for r in requirements)
    )

    result = run_agent_prompt(
        agent_id,
        (
            f"Classify each requirement below into the most appropriate document section.\n\n"
            f"Available sections: {json.dumps(section_names)}\n\n"
            f"Requirements:\n{req_summaries}\n\n"
            f"Return ONLY a JSON object mapping section names to arrays of requirement IDs.\n"
            f"Example: {{\"Executive Summary\": [\"F1\",\"F2\"], \"Technical Approach\": [\"F3\"]}}\n"
            f"Every requirement must appear in exactly one section."
        ),
    )

    section_map = (
        _parse_section_map(result.text, section_names, [r.get("id", "?") for r in requirements])
        if result.completed
        else {}
    )
    # Fallback: if classification failed, put all in first section
    if not section_map:
        section_map = {section_names[0]: [r.get("id", "?") for r in requirements]}

    # Build lookup
    req_by_id = {r.get("id", "?"): r for r in requirements}

    yield {"type": "section_map", "data": section_map}
    logger.info("Section mapping: %s", {k: len(v) for k, v in section_map.items()})

    # Step 2: Generate narrative per section (optionally parallel)
    section_jobs: list[tuple[str, list[str], list[dict[str, Any]]]] = []
    for section_name, req_ids in section_map.items():
        if not req_ids:
            continue
        section_reqs = [req_by_id[rid] for rid in req_ids if rid in req_by_id]
        if not section_reqs:
            continue
        section_jobs.append((section_name, req_ids, section_reqs))

    if section_workers <= 1 or len(section_jobs) <= 1:
        for section_name, req_ids, section_reqs in section_jobs:
            logger.info("Generating section '%s' with %d requirements", section_name, len(section_reqs))
            section_data = _generate_section_narrative(
                section_name=section_name,
                req_ids=req_ids,
                section_reqs=section_reqs,
                context=section_context(req_ids),
                tone=tone,
                agent_id=agent_id,
            )
            yield {"type": "section_narrative", "data": section_data}
    else:
        logger.info("Generating %d sections in parallel (workers=%d)", len(section_jobs), section_workers)
        with concurrent.futures.ThreadPoolExecutor(max_workers=section_workers) as pool:
            futures = [
                pool.submit(
                    _generate_section_narrative,
                    section_name=section_name,
                    req_ids=req_ids,
                    section_reqs=section_reqs,
                    context=section_context(req_ids),
                    tone=tone,
                    agent_id=agent_id,
                )
                for section_name, req_ids, section_reqs in section_jobs
            ]
            for fut in concurrent.futures.as_completed(futures):
                try:
                    section_data = fut.result()
                except Exception as exc:
                    logger.error("Parallel section generation failed: %s", exc)
                    section_data = {
                        "section_name": "unknown",
                        "narrative_markdown": f"Section generation failed: {exc}",
                        "requirement_ids": [],
                    }
                yield {"type": "section_narrative", "data": section_data}

    # Step 3: Generate compliance matrix for all requirements
    logger.info("Generating compliance matrix for %d requirements", len(requirements))
    total_requirements = len(requirements)
    for idx, req in enumerate(requirements, start=1):
        yield {
            "type": "requirement_started",
            "data": {
                "requirement_id": req.get("id", "?"),
                "index": idx,
                "total": total_requirements,
            },
        }
        try:
            result = _answer_single_requirement(
                requirement=req,
                context=contexts[idx - 1],
                tone=tone,
                agent_id=agent_id,
                user_context=user_context,
                allow_web_search=allow_web_search,
            )
            # Only keep compliance fields, not full narrative
            yield {
                "type": "requirement_compliance",
                "data": {
                    "requirement_id": result.get("requirement_id", req.get("id", "?")),
                    "meets": result.get("meets", "No"),
                    "answer_markdown": result.get("answer_markdown", ""),
                    "evidence": result.get("evidence", []),
                },
            }
        except Exception as exc:
            yield {
                "type": "requirement_compliance",
                "data": {
                    "requirement_id": req.get("id", "?"),
                    "meets": "No",
                    "answer_markdown": f"Error: {exc}",
                    "evidence": [],
                },
            }


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
):
    """Approach 3: Map-Reduce — answer each requirement, then synthesize into sections.

    Phase 1 (Map): Process each requirement individually → yields 'response' events.
    Phase 2 (Reduce): Group answers by template section, generate narratives → yields 'section_narrative' events.

    This separates accuracy (Phase 1) from presentation (Phase 2).
    """
    settings = get_settings()
    agent_id = _ensure_agent(_DRAFT_AGENT_NAME)
    section_workers = max(1, settings.section_parallelism)

    # Retrieve per requirement (on demand, in Phase 1)
    retriever = kb_search.RequirementRetriever(
        requirements,
        client_id=client_id,
        project_id=project_id,
        solution_area=solution_area,
        user_oid=user_oid,
        user_groups=user_groups,
    )

    section_names = [s["heading"] for s in template_sections] if template_sections else [
        "Executive Summary", "Solution Overview", "Technical Approach",
        "Compliance & Security", "Implementation & Support",
    ]

    # ── Phase 1: Map — answer every requirement individually ──
    logger.info("Phase 1: Answering %d requirements individually", len(requirements))
    yield {"type": "phase", "data": {"phase": 1, "description": "Answering each requirement individually"}}

    all_responses: list[dict[str, Any]] = []
    for i, req in enumerate(requirements):
        logger.info("Phase 1: Requirement %d/%d: %s", i + 1, len(requirements), req.get("id", "?"))
        try:
            result = _answer_single_requirement(
                requirement=req,
                context=retriever.context(i),
                tone=tone,
                agent_id=agent_id,
                user_context=user_context,
                allow_web_search=allow_web_search,
            )
            all_responses.append(result)
            yield {"type": "response", "data": result}
        except Exception as exc:
            logger.error("Error on requirement %s: %s", req.get("id", "?"), exc)
            error_resp = {
                "requirement_id": req.get("id", "?"),
                "requirement_text": req.get("text", ""),
                "answer_markdown": f"Error: {exc}",
                "meets": "No",
                "evidence": [],
                "assumptions": [],
                "risks": [str(exc)],
                "followups": [],
            }
            all_responses.append(error_resp)
            yield {"type": "response", "data": error_resp}

    # ── Phase 2: Reduce — classify and synthesize into sections ──
    logger.info("Phase 2: Synthesizing %d answers into %d sections", len(all_responses), len(section_names))
    yield {"type": "phase", "data": {"phase": 2, "description": "Synthesizing answers into template sections"}}

    # Step 2a: Classify requirements into sections
    req_summaries = fence(
        "REQUIREMENTS",
        "\n".join(
            f"- {r.get('requirement_id', '?')}: {r.get('requirement_text', r.get('text', ''))[:100]}"
            for r in all_responses
        ),
    )

    result = run_agent_prompt(
        agent_id,
        (
            f"Classify each requirement into the best document section.\n\n"
            f"Sections: {json.dumps(section_names)}\n\n"
            f"Requirements:\n{req_summaries}\n\n"
            f"Return ONLY a JSON object mapping section names to arrays of requirement IDs.\n"
            f"Every requirement must appear in exactly one section.\n"
            f"Example: {{\"Executive Summary\": [\"F1\"], \"Technical Approach\": [\"F2\",\"F3\"]}}"
        ),
    )

    section_map = (
        _parse_section_map(result.text, section_names, [r.get("requirement_id", "?") for r in all_responses])
        if result.completed
        else {}
    )
    if not section_map:
        section_map = {section_names[0]: [r.get("requirement_id", "?") for r in all_responses]}

    yield {"type": "section_map", "data": section_map}

    # Build response lookup
    resp_by_id = {r.get("requirement_id", ""): r for r in all_responses}

    # Step 2b: For each section, synthesize a narrative from the pre-generated answers
    section_jobs: list[tuple[str, list[str], list[dict[str, Any]]]] = []
    for section_name, req_ids in section_map.items():
        if not req_ids:
            continue
        section_answers = [resp_by_id[rid] for rid in req_ids if rid in resp_by_id]
        if not section_answers:
            continue
        section_jobs.append((section_name, req_ids, section_answers))

    if section_workers <= 1 or len(section_jobs) <= 1:
        for section_name, req_ids, section_answers in section_jobs:
            logger.info("Phase 2: Writing section '%s' from %d answers", section_name, len(section_answers))
            section_data = _generate_section_narrative_from_answers(
                section_name=section_name,
                req_ids=req_ids,
                section_answers=section_answers,
                tone=tone,
                agent_id=agent_id,
            )
            yield {"type": "section_narrative", "data": section_data}
    else:
        logger.info("Phase 2: Generating %d sections in parallel (workers=%d)", len(section_jobs), section_workers)
        with concurrent.futures.ThreadPoolExecutor(max_workers=section_workers) as pool:
            futures = [
                pool.submit(
                    _generate_section_narrative_from_answers,
                    section_name=section_name,
                    req_ids=req_ids,
                    section_answers=section_answers,
                    tone=tone,
                    agent_id=agent_id,
                )
                for section_name, req_ids, section_answers in section_jobs
            ]
            for fut in concurrent.futures.as_completed(futures):
                try:
                    section_data = fut.result()
                except Exception as exc:
                    logger.error("Parallel map-reduce section generation failed: %s", exc)
                    section_data = {
                        "section_name": "unknown",
                        "narrative_markdown": f"Section generation failed: {exc}",
                        "requirement_ids": [],
                    }
                yield {"type": "section_narrative", "data": section_data}


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

    # Get or create the persistent QA agent
    agent_id = _ensure_agent(_QA_AGENT_NAME)

    result = run_agent_prompt(
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

    safe_text, violations = _post_generation_guard(result.text)
    allowed_urls = context.urls | result.annotation_urls

    try:
        parsed = json.loads(_strip_code_fence(safe_text))
        if not isinstance(parsed, dict):
            raise ValueError("expected a JSON object")
        answer = kb_search.sanitize_citations(normalize_answer(parsed, require_meets=False), allowed_urls)
    except (ValueError, IndexError):
        answer = {"raw_response": kb_search.sanitize_citations(safe_text, allowed_urls), "parse_error": True}
    if isinstance(answer, dict):
        answer["kb_evidence_found"] = context.found
    return answer
