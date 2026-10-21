"""Running prompts on Foundry agents and reading what comes back."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from azure.ai.agents import AgentsClient
from azure.ai.agents.models import BingGroundingTool, MessageRole

from app import azure_clients
from app.config import get_settings
from app.ingestion.redaction import get_redaction_engine

logger = logging.getLogger(__name__)


class WebSearchNotConfigured(ValueError):
    """Web grounding was requested but no Grounding-with-Bing connection is configured."""


def web_search_available() -> bool:
    return bool(get_settings().bing_connection_id)


def _web_search_tools() -> list[Any]:
    connection_id = get_settings().bing_connection_id
    if not connection_id:
        raise WebSearchNotConfigured("Web search is not configured: set BING_CONNECTION_ID.")
    return BingGroundingTool(connection_id=connection_id).definitions


# ── Output checks ─────────────────────────────────────────────


def post_generation_guard(text: str) -> tuple[str, list[dict]]:
    """Run the redaction engine's output scanner.  Returns (safe_text, violations).

    Violations are redacted and the response is still returned.
    TODO: hand output that trips the scanner to a reviewing agent that can rewrite or withhold it,
    instead of only masking the matched spans (tracked in the "Agent-based review" issue).
    """
    engine = get_redaction_engine()
    violations = engine.scan_output(text)
    if violations:
        logger.warning("Post-generation guard caught %d violations", len(violations))
        return engine.redact(text).sanitized_text, violations
    return text, []


def strip_code_fence(text: str) -> str:
    """The JSON inside a ```json fence if the model added one, else the text itself."""
    if "```json" in text:
        text = text.split("```json", 1)[1].split("```", 1)[0]
    elif "```" in text:
        text = text.split("```", 1)[1].split("```", 1)[0]
    return text.strip()


def parse_json_object(text: str) -> dict[str, Any]:
    """The JSON object in a model reply; ValueError if the reply isn't one."""
    parsed = json.loads(strip_code_fence(text))
    if not isinstance(parsed, dict):
        raise ValueError("expected a JSON object")
    return parsed


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
    settings = get_settings()
    client = azure_clients.agents_client(settings)
    timeout = settings.run_timeout_seconds
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
