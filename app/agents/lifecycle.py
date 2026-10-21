"""The persistent Foundry agents: found by name, created on first use, kept up to date.

Agents are **singletons**: created once and reused for every request. If the instructions change the agent
is updated in place. Agents carry no tools; web grounding is attached to individual runs that ask for it
(see :func:`app.agents.runner.run_agent_prompt`).
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from azure.ai.agents import AgentsClient

from app import azure_clients
from app.agents.prompts import AGENT_INSTRUCTIONS
from app.config import get_settings

logger = logging.getLogger(__name__)

DRAFT_AGENT_NAME = "rfp-draft-agent"
QA_AGENT_NAME = "rfp-qa-agent"

_draft_agent_id: str | None = None
_qa_agent_id: str | None = None
_current_instructions_hash: str | None = None


def _instructions_hash() -> str:
    """Hash of current instructions to detect changes."""
    return hashlib.sha256(AGENT_INSTRUCTIONS.encode()).hexdigest()[:12]


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


def ensure_agent(name: str) -> str:
    """Get or create a persistent agent by name and return its ID.

    If instructions have changed since the agent was last created/updated, the agent is updated in
    place. If creation fails, raises with a clear error.
    """
    global _draft_agent_id, _qa_agent_id, _current_instructions_hash

    settings = get_settings()
    client = azure_clients.agents_client(settings)
    new_hash = _instructions_hash()

    cached_id = _draft_agent_id if name == DRAFT_AGENT_NAME else _qa_agent_id

    # If we have a cached ID and instructions haven't changed, verify it still exists
    if cached_id and _current_instructions_hash == new_hash:
        try:
            agent = client.get_agent(cached_id)
            if agent and agent.id:
                return cached_id
        except Exception:
            logger.info("Cached agent %s no longer exists, will recreate", cached_id)

    existing_id = _find_existing_agent(client, name)

    if existing_id:
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

    if name == DRAFT_AGENT_NAME:
        _draft_agent_id = agent_id
    else:
        _qa_agent_id = agent_id
    _current_instructions_hash = new_hash

    return agent_id


def setup_agents() -> dict[str, str]:
    """Pre-create (or verify) all persistent agents at startup. Returns agent name → agent ID."""
    logger.info("Setting up persistent agents...")
    draft_id = ensure_agent(DRAFT_AGENT_NAME)
    qa_id = ensure_agent(QA_AGENT_NAME)
    logger.info("Agents ready: draft=%s, qa=%s", draft_id, qa_id)
    return {DRAFT_AGENT_NAME: draft_id, QA_AGENT_NAME: qa_id}
