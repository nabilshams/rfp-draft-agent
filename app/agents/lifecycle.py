"""The persistent Foundry agents: found by name, created on first use, kept up to date.

Agents are **singletons**: created once and reused for every request. If the instructions (or the model) change
the agent is updated in place. Agents carry no tools; web grounding is attached to individual runs that ask for it
(see :func:`app.agents.runner.run_agent_prompt`).
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from azure.ai.agents import AgentsClient

from app import azure_clients
from app.agents.prompts import AGENT_INSTRUCTIONS, VERIFIER_INSTRUCTIONS
from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

DRAFT_AGENT_NAME = "rfp-draft-agent"
QA_AGENT_NAME = "rfp-qa-agent"
VERIFIER_AGENT_NAME = "rfp-verifier-agent"


@dataclass(frozen=True)
class AgentSpec:
    """What an agent is: its instructions and the model deployment it runs on."""

    instructions: str
    model: Callable[[Settings], str]


def _chat_model(settings: Settings) -> str:
    return settings.foundry_model_deployment_name


def _verifier_model(settings: Settings) -> str:
    """The verifier may run on another deployment, to reduce errors it would share with the writer."""
    return settings.verifier_model_deployment_name or settings.foundry_model_deployment_name


SPECS: dict[str, AgentSpec] = {
    DRAFT_AGENT_NAME: AgentSpec(AGENT_INSTRUCTIONS, _chat_model),
    QA_AGENT_NAME: AgentSpec(AGENT_INSTRUCTIONS, _chat_model),
    VERIFIER_AGENT_NAME: AgentSpec(VERIFIER_INSTRUCTIONS, _verifier_model),
}

# agent name -> id, and the fingerprint (instructions and model) the agent was last brought up to date with
_agent_ids: dict[str, str] = {}
_agent_hashes: dict[str, str] = {}


def _name_of(agent_id: str) -> str | None:
    return next((name for name, known in _agent_ids.items() if known == agent_id), None)


def model_of(agent_id: str, settings: Settings) -> str:
    """The deployment an agent runs on (the writer's for an agent this module does not know)."""
    name = _name_of(agent_id)
    return SPECS[name].model(settings) if name else settings.foundry_model_deployment_name


def prices_of(agent_id: str, settings: Settings) -> tuple[float, float] | None:
    """The (input, output) price per million tokens for an agent whose model is priced differently, else ``None``."""
    if _name_of(agent_id) == VERIFIER_AGENT_NAME and settings.verifier_input_price_per_million is not None:
        return settings.verifier_input_price_per_million, settings.verifier_output_price_per_million or 0.0
    return None


def reset_cache() -> None:
    _agent_ids.clear()
    _agent_hashes.clear()


def _fingerprint(spec: AgentSpec, settings: Settings) -> str:
    """Hash of an agent's current instructions and model, to detect changes."""
    return hashlib.sha256(f"{spec.model(settings)}\n{spec.instructions}".encode()).hexdigest()[:12]


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

    If its instructions or model have changed since the agent was last created/updated, the agent is updated in
    place. If creation fails, raises with a clear error.
    """
    spec = SPECS[name]
    settings = get_settings()
    client = azure_clients.agents_client(settings)
    new_hash = _fingerprint(spec, settings)
    model = spec.model(settings)

    cached_id = _agent_ids.get(name)

    # If we have a cached ID and nothing has changed, verify it still exists
    if cached_id and _agent_hashes.get(name) == new_hash:
        try:
            agent = client.get_agent(cached_id)
            if agent and agent.id:
                return cached_id
        except Exception:
            logger.info("Cached agent %s no longer exists, will recreate", cached_id)

    existing_id = _find_existing_agent(client, name)

    if existing_id:
        if _agent_hashes.get(name) != new_hash:
            try:
                client.update_agent(agent_id=existing_id, model=model, name=name, instructions=spec.instructions)
                logger.info("Updated agent '%s' (%s) with new instructions", name, existing_id)
            except Exception as exc:
                logger.warning("Could not update agent '%s': %s", name, exc)

        _ensure_no_standing_web_tool(client, existing_id, name)
        agent_id = existing_id
    else:
        logger.info("No existing agent '%s' found, creating new one...", name)
        try:
            agent = client.create_agent(model=model, name=name, instructions=spec.instructions)
        except Exception as exc:
            logger.error("Failed to create agent '%s': %s", name, exc)
            raise RuntimeError(f"Cannot create or find agent '{name}': {exc}") from exc
        agent_id = agent.id
        logger.info("Created persistent agent '%s' → %s", name, agent_id)

    _agent_ids[name] = agent_id
    _agent_hashes[name] = new_hash
    return agent_id


def setup_agents() -> dict[str, str]:
    """Pre-create (or verify) all persistent agents at startup. Returns agent name → agent ID."""
    logger.info("Setting up persistent agents...")
    names = [DRAFT_AGENT_NAME, QA_AGENT_NAME]
    if get_settings().verify_answers:
        names.append(VERIFIER_AGENT_NAME)
    ids = {name: ensure_agent(name) for name in names}
    logger.info("Agents ready: %s", ", ".join(f"{name}={agent_id}" for name, agent_id in ids.items()))
    return ids
