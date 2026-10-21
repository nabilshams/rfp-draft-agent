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
import os
import time
from dataclasses import dataclass, field
from typing import Any

from azure.ai.agents import AgentsClient
from azure.ai.agents.models import BingGroundingTool, MessageRole
from azure.identity import DefaultAzureCredential
from azure.search.documents import SearchClient

from app.agents.prompts import AGENT_INSTRUCTIONS
from app.agents.retrieval import build_odata_filter
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
# Full resource ID of the project's Grounding-with-Bing connection. Bing grounding is skipped when unset.
_BING_CONNECTION_ID = os.environ.get("BING_CONNECTION_ID", "")


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


def _ensure_agent(name: str) -> str:
    """Get or create a persistent agent by name.

    If instructions have changed since the agent was last created/updated,
    the agent is updated in place. If creation fails, raises with a clear error.

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

        agent_id = existing_id
    else:
        # Create new agent with Bing Grounding tool
        logger.info("No existing agent '%s' found, creating new one...", name)
        try:
            if not _BING_CONNECTION_ID:
                raise ValueError("BING_CONNECTION_ID not set; creating agent without Bing grounding")
            bing_tool = BingGroundingTool(connection_id=_BING_CONNECTION_ID)
            agent = client.create_agent(
                model=settings.foundry_model_deployment_name,
                name=name,
                instructions=AGENT_INSTRUCTIONS,
                tools=bing_tool.definitions,
            )
            agent_id = agent.id
            logger.info("Created persistent agent '%s' → %s (with Bing grounding)", name, agent_id)
        except Exception as exc:
            logger.error("Failed to create agent '%s': %s", name, exc)
            # Try without Bing tool as fallback
            try:
                agent = client.create_agent(
                    model=settings.foundry_model_deployment_name,
                    name=name,
                    instructions=AGENT_INSTRUCTIONS,
                )
                agent_id = agent.id
                logger.info("Created agent '%s' → %s (without Bing, fallback)", name, agent_id)
            except Exception as exc2:
                logger.error("Fallback create also failed for '%s': %s", name, exc2)
                raise RuntimeError(f"Cannot create or find agent '{name}': {exc2}") from exc2

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


def _search_knowledge_base(
    *,
    query: str,
    client_id: str,
    project_id: str,
    solution_area: str | None,
    user_oid: str,
    user_groups: list[str] | None,
    top: int = 8,
) -> list[dict[str, Any]]:
    """Run a hybrid search against AI Search with DLAC + segmentation filters.

    Returns a list of dicts with keys: content, title, source_url, score.
    """
    settings = get_settings()
    search_endpoint = settings.azure_ai_search_endpoint or (
        f"https://{settings.azure_ai_search_connection_name}.search.windows.net"
    )

    odata_filter = build_odata_filter(
        client_id=client_id,
        project_id=project_id,
        solution_area=solution_area,
        user_oid=user_oid,
        user_groups=user_groups,
    )

    search_client = SearchClient(
        endpoint=search_endpoint,
        index_name=settings.ai_search_index_name,
        credential=DefaultAzureCredential(),
    )

    results = search_client.search(
        search_text=query,
        filter=odata_filter,
        top=top,
        select=["content", "title", "source_url"],
    )

    docs = []
    for r in results:
        docs.append({
            "content": r.get("content", ""),
            "title": r.get("title", ""),
            "source_url": r.get("source_url", ""),
            "score": r.get("@search.score", 0),
        })
    logger.info("Search returned %d results for query: %.60s", len(docs), query)
    return docs



def _format_context(kb_docs: list[dict[str, Any]]) -> str:
    """Format retrieved KB documents into a context block for the agent prompt."""
    if not kb_docs:
        return "\n## Internal Knowledge Base\n[No matching documents found in KB.]\n"

    parts = ["## Internal Knowledge Base Sources\n"]
    for i, doc in enumerate(kb_docs, 1):
        parts.append(f"### KB Source {i}: {doc['title']}")
        parts.append(f"**URL:** {doc['source_url']}")
        parts.append(f"**Content:**\n{doc['content'][:1500]}\n")
    return "\n".join(parts)


def _validate_citations(response_text: str, retrieved_urls: set[str]) -> str:
    """Strip any citations that reference URLs not actually retrieved."""
    # Lightweight check: look for markdown-link patterns and verify
    import re

    def _check_link(m: re.Match) -> str:
        url = m.group(2)
        if url in retrieved_urls or url.startswith("#"):
            return m.group(0)
        logger.warning("Removing fabricated citation URL: %s", url)
        return m.group(1)  # keep the link text, drop the URL

    return re.sub(r"\[([^\]]+)\]\(([^)]+)\)", _check_link, response_text)


def _post_generation_guard(text: str) -> tuple[str, list[dict]]:
    """Run the redaction engine's output scanner.  Returns (safe_text, violations)."""
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


def run_agent_prompt(agent_id: str, content: str) -> AgentRunResult:
    """Send *content* to an agent on a new thread, wait for the run, and return its reply.

    Blocking: call from a worker thread, not from the event loop. The run is cancelled if
    it exceeds ``RUN_TIMEOUT_SECONDS`` (status ``timed_out``), and the thread is always deleted.
    """
    client = _get_agents_client()
    timeout = get_settings().run_timeout_seconds
    thread = client.threads.create()
    try:
        client.messages.create(thread_id=thread.id, role=MessageRole.USER, content=content)
        run = client.runs.create(thread_id=thread.id, agent_id=agent_id)
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
                        if hasattr(ann, "url"):
                            result.annotation_urls.add(ann.url)
        return result
    finally:
        _delete_thread(client, thread.id)


# ── Public API ────────────────────────────────────────────────


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
) -> dict[str, Any]:
    """Create an agent, submit the full RFP, and return the draft JSON."""

    # Step 1: Retrieve relevant knowledge base content
    search_query = requirements_json[:500]  # Use requirements as search query
    retrieved_docs = _search_knowledge_base(
        query=search_query,
        client_id=client_id,
        project_id=project_id,
        solution_area=solution_area,
        user_oid=user_oid,
        user_groups=user_groups,
    )

    # Step 1b: Search Microsoft documentation

    context_block = _format_context(retrieved_docs)

    # Collect retrieved URLs for citation hygiene
    all_docs = retrieved_docs
    retrieved_urls = {d["source_url"] for d in all_docs if d.get("source_url")}

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
        f"{context_block}\n\n"
        f"Here are the requirements to respond to:\n{requirements_json}"
    )

    result = run_agent_prompt(agent_id, user_message)

    if not result.completed:
        logger.error("Agent run failed: %s – %s", result.status, result.last_error)
        return {"error": f"Agent run ended with status: {result.status}"}

    if not result.has_response:
        return {"error": "No assistant response received"}

    raw_text = result.text
    retrieved_urls = result.annotation_urls

    # Citation hygiene
    clean_text = _validate_citations(raw_text, retrieved_urls)

    # Post-generation guardrail
    safe_text, violations = _post_generation_guard(clean_text)

    if violations:
        logger.warning("Draft contained %d sensitive patterns – redacted", len(violations))

    # Parse JSON from the agent's response
    try:
        # The agent may wrap JSON in ```json ... ```
        json_str = safe_text
        if "```json" in json_str:
            json_str = json_str.split("```json", 1)[1].split("```", 1)[0]
        elif "```" in json_str:
            json_str = json_str.split("```", 1)[1].split("```", 1)[0]
        draft = json.loads(json_str.strip())
    except (json.JSONDecodeError, IndexError):
        draft = {"raw_response": safe_text, "parse_error": True}

    draft["_meta"] = {
        "rfp_id": rfp_id,
        "agent_id": agent_id,
        "run_id": result.run_id,
        "violations_redacted": len(violations),
    }
    return draft


def _answer_single_requirement(
    *,
    requirement: dict[str, Any],
    context_block: str,
    tone: str,
    agent_id: str,
    user_context: str = "",
) -> dict[str, Any]:
    """Process a single requirement through the agent and return structured response."""
    req_id = requirement.get("id", "?")
    req_text = requirement.get("text", "")

    context_preamble = ""
    if user_context:
        context_preamble = f"## Company / Response Context\n{user_context}\n\n"

    result = run_agent_prompt(
        agent_id,
        (
            f"Respond to this single RFP requirement. Tone: {tone}.\n\n"
            f"{context_preamble}"
            f"Return ONLY a JSON object (no markdown wrapping) with these fields:\n"
            f"- \"requirement_id\": \"{req_id}\"\n"
            f"- \"requirement_text\": the original requirement text\n"
            f"- \"answer_markdown\": your detailed response (2-4 paragraphs)\n"
            f"- \"meets\": \"Yes\", \"Partial\", or \"No\"\n"
            f"- \"evidence\": array of {{\"title\", \"source_url\", \"quote\"}}\n"
            f"- \"assumptions\": array of strings\n"
            f"- \"risks\": array of strings\n"
            f"- \"followups\": array of strings\n\n"
            f"Ground your answer using these sources:\n\n"
            f"{context_block}\n\n"
            f"Requirement ID: {req_id}\n"
            f"Category: {requirement.get('category', 'general')}\n"
            f"Priority: {requirement.get('priority', 'standard')}\n"
            f"Requirement: {req_text}"
        ),
    )

    if not result.completed:
        logger.error("Requirement %s run failed: %s", req_id, result.status)
        return {
            "requirement_id": req_id,
            "requirement_text": req_text,
            "answer_markdown": f"Agent run failed with status: {result.status}",
            "meets": "No",
            "evidence": [],
            "assumptions": [],
            "risks": ["Agent processing failed"],
            "followups": [],
        }

    safe_text, _ = _post_generation_guard(result.text)

    try:
        json_str = safe_text
        if "```json" in json_str:
            json_str = json_str.split("```json", 1)[1].split("```", 1)[0]
        elif "```" in json_str:
            json_str = json_str.split("```", 1)[1].split("```", 1)[0]
        result = json.loads(json_str.strip())
        result["requirement_id"] = req_id
        return result
    except (json.JSONDecodeError, IndexError):
        return {
            "requirement_id": req_id,
            "requirement_text": req_text,
            "answer_markdown": safe_text,
            "meets": "Partial",
            "evidence": [],
            "assumptions": [],
            "risks": [],
            "followups": [],
        }


def _generate_section_narrative(
    *,
    section_name: str,
    req_ids: list[str],
    section_reqs: list[dict[str, Any]],
    context_block: str,
    tone: str,
    agent_id: str,
) -> dict[str, Any]:
    """Generate one section narrative from a list of requirements."""

    req_details = "\n".join(
        f"- [{r.get('id','?')}] ({r.get('priority','standard')}): {r.get('text','')}"
        for r in section_reqs
    )

    result = run_agent_prompt(
        agent_id,
        (
            f"Write the \"{section_name}\" section of an RFP response document. "
            f"Tone: {tone}.\n\n"
            f"This section must address the following requirements:\n{req_details}\n\n"
            f"Write a coherent, professional narrative (not a list of individual answers). "
            f"Weave the requirements naturally into the narrative. "
            f"Reference requirement IDs in brackets where relevant, e.g. [F1].\n\n"
            f"Use these sources for grounding:\n{context_block}\n\n"
            f"Return ONLY a JSON object:\n"
            f"{{\"section_name\": \"{section_name}\", "
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
    try:
        text = safe
        if "```json" in text:
            text = text.split("```json", 1)[1].split("```", 1)[0]
        elif "```" in text:
            text = text.split("```", 1)[1].split("```", 1)[0]
        section_data = json.loads(text.strip())
    except Exception:
        section_data = {
            "section_name": section_name,
            "narrative_markdown": safe,
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

    answers_digest = "\n\n".join(
        f"### [{a.get('requirement_id', '?')}] (Compliance: {a.get('meets', '?')})\n"
        f"Requirement: {a.get('requirement_text', '')[:200]}\n"
        f"Response: {a.get('answer_markdown', '')}"
        for a in section_answers
    )

    result = run_agent_prompt(
        agent_id,
        (
            f"Write the \"{section_name}\" section of a professional RFP response document.\n"
            f"Tone: {tone}.\n\n"
            f"Below are the individual requirement responses that belong in this section. "
            f"Your job is to synthesize them into a coherent, flowing narrative — NOT a list of "
            f"individual answers. Weave them together naturally. Reference requirement IDs "
            f"in brackets where relevant, e.g. [F1].\n\n"
            f"Keep all factual claims from the original answers. Do not add new information.\n\n"
            f"--- Answers for this section ---\n\n{answers_digest}\n\n"
            f"Return ONLY a JSON object:\n"
            f"{{\"section_name\": \"{section_name}\", "
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
    try:
        text = safe
        if "```json" in text:
            text = text.split("```json", 1)[1].split("```", 1)[0]
        elif "```" in text:
            text = text.split("```", 1)[1].split("```", 1)[0]
        section_data = json.loads(text.strip())
    except Exception:
        section_data = {
            "section_name": section_name,
            "narrative_markdown": safe,
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
):
    """Approach 1: Process each requirement independently.

    Yields one response dict per requirement with requirement_id, answer, meets, etc.
    Good for: compliance-heavy RFPs where each requirement needs individual assessment.
    """
    # Retrieve context once (shared across all requirements)
    combined_text = " ".join(r.get("text", "")[:100] for r in requirements[:10])
    retrieved_docs = _search_knowledge_base(
        query=combined_text[:500],
        client_id=client_id,
        project_id=project_id,
        solution_area=solution_area,
        user_oid=user_oid,
        user_groups=user_groups,
    )
    context_block = _format_context(retrieved_docs)

    agent_id = _ensure_agent(_DRAFT_AGENT_NAME)
    logger.info("Streaming draft: %d requirements, agent=%s", len(requirements), agent_id)

    for i, req in enumerate(requirements):
        logger.info("Processing requirement %d/%d: %s", i + 1, len(requirements), req.get("id", "?"))
        try:
            result = _answer_single_requirement(
                requirement=req,
                context_block=context_block,
                tone=tone,
                agent_id=agent_id,
                user_context=user_context,
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

    # Retrieve context once
    combined_text = " ".join(r.get("text", "")[:100] for r in requirements[:10])
    retrieved_docs = _search_knowledge_base(
        query=combined_text[:500],
        client_id=client_id,
        project_id=project_id,
        solution_area=solution_area,
        user_oid=user_oid,
        user_groups=user_groups,
    )
    context_block = _format_context(retrieved_docs)

    section_names = [s["heading"] for s in template_sections] if template_sections else [
        "Executive Summary", "Solution Overview", "Technical Approach",
        "Compliance & Security", "Implementation & Support",
    ]

    # Step 1: Classify requirements into sections
    logger.info("Step 1: Classifying %d requirements into %d sections", len(requirements), len(section_names))
    req_summaries = "\n".join(
        f"- {r.get('id','?')}: {r.get('text','')[:120]}" for r in requirements
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

    section_map: dict[str, list[str]] = {}
    if result.completed:
        try:
            text = result.text
            if "```json" in text:
                text = text.split("```json", 1)[1].split("```", 1)[0]
            elif "```" in text:
                text = text.split("```", 1)[1].split("```", 1)[0]
            section_map = json.loads(text.strip())
        except Exception:
            pass

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
                context_block=context_block,
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
                    context_block=context_block,
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
                context_block=context_block,
                tone=tone,
                agent_id=agent_id,
                user_context=user_context,
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
):
    """Approach 3: Map-Reduce — answer each requirement, then synthesize into sections.

    Phase 1 (Map): Process each requirement individually → yields 'response' events.
    Phase 2 (Reduce): Group answers by template section, generate narratives → yields 'section_narrative' events.

    This separates accuracy (Phase 1) from presentation (Phase 2).
    """
    settings = get_settings()
    agent_id = _ensure_agent(_DRAFT_AGENT_NAME)
    section_workers = max(1, settings.section_parallelism)

    # Retrieve context once
    combined_text = " ".join(r.get("text", "")[:100] for r in requirements[:10])
    retrieved_docs = _search_knowledge_base(
        query=combined_text[:500],
        client_id=client_id,
        project_id=project_id,
        solution_area=solution_area,
        user_oid=user_oid,
        user_groups=user_groups,
    )
    context_block = _format_context(retrieved_docs)

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
                context_block=context_block,
                tone=tone,
                agent_id=agent_id,
                user_context=user_context,
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
    req_summaries = "\n".join(
        f"- {r.get('requirement_id', '?')}: {r.get('requirement_text', r.get('text', ''))[:100]}"
        for r in all_responses
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

    section_map: dict[str, list[str]] = {}
    if result.completed:
        try:
            text = result.text
            if "```json" in text:
                text = text.split("```json", 1)[1].split("```", 1)[0]
            elif "```" in text:
                text = text.split("```", 1)[1].split("```", 1)[0]
            section_map = json.loads(text.strip())
        except Exception:
            pass

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
) -> dict[str, Any]:
    """Answer a single question using the grounded agent."""

    # Retrieve relevant context
    retrieved_docs = _search_knowledge_base(
        query=question,
        client_id=client_id,
        project_id=project_id,
        solution_area=solution_area,
        user_oid=user_oid,
        user_groups=user_groups,
    )

    # Also search Microsoft documentation
    context_block = _format_context(retrieved_docs)

    # Get or create the persistent QA agent
    agent_id = _ensure_agent(_QA_AGENT_NAME)

    result = run_agent_prompt(
        agent_id,
        (
            f"Answer the following question using ONLY the retrieved sources below.  "
            f"Return a JSON object with answer_markdown, evidence, assumptions, "
            f"and followups.\n\n"
            f"{context_block}\n\n"
            f"Question: {question}"
        ),
    )

    if not result.completed:
        return {"error": f"Agent run ended with status: {result.status}"}

    safe_text, violations = _post_generation_guard(result.text)

    try:
        json_str = safe_text
        if "```json" in json_str:
            json_str = json_str.split("```json", 1)[1].split("```", 1)[0]
        elif "```" in json_str:
            json_str = json_str.split("```", 1)[1].split("```", 1)[0]
        return json.loads(json_str.strip())
    except (json.JSONDecodeError, IndexError):
        return {"raw_response": safe_text, "parse_error": True}
