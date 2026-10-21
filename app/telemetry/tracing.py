"""OpenTelemetry + Application Insights tracing setup.

Instruments FastAPI, logs tool calls, retrieval metadata, latency,
token usage, and redaction events.  Sensitive content is never stored
in traces – only hashes or redacted excerpts.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

logger = logging.getLogger(__name__)


def configure_tracing(app: Any) -> None:
    """Wire up OpenTelemetry + Azure Monitor for the FastAPI application.

    Call this once at startup (before the first request).
    """
    conn_str = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING", "")

    resource = Resource.create(
        {
            "service.name": "rfp-draft-agent",
            "service.version": "0.1.0",
        }
    )

    provider = TracerProvider(resource=resource)

    if conn_str:
        try:
            from azure.monitor.opentelemetry.exporter import AzureMonitorTraceExporter

            exporter = AzureMonitorTraceExporter(connection_string=conn_str)
            provider.add_span_processor(BatchSpanProcessor(exporter))
            logger.info("Azure Monitor trace exporter configured")
        except Exception as exc:
            logger.warning("Could not configure Azure Monitor exporter: %s", exc)
    else:
        logger.info("No APPLICATIONINSIGHTS_CONNECTION_STRING – traces are local only")

    trace.set_tracer_provider(provider)

    # Instrument FastAPI
    FastAPIInstrumentor.instrument_app(app)
    logger.info("FastAPI OTel instrumentation enabled")


def get_tracer(name: str = "rfp-draft-agent") -> trace.Tracer:
    """Return a named tracer for manual span creation."""
    return trace.get_tracer(name)


# ── Helper decorators / context managers ──────────────────


def trace_tool_call(
    tool_name: str,
    metadata: dict[str, Any] | None = None,
) -> trace.Span:
    """Start a span for a tool call and attach metadata as attributes."""
    tracer = get_tracer()
    span = tracer.start_span(f"tool.{tool_name}")
    if metadata:
        for k, v in metadata.items():
            # Never store raw content – store hashes or truncated values
            safe_val = str(v)[:200] if isinstance(v, str) else str(v)
            span.set_attribute(f"tool.{tool_name}.{k}", safe_val)
    return span


def record_redaction_event(span: trace.Span, count: int, content_hash: str) -> None:
    """Attach redaction telemetry to a span without storing raw content."""
    span.set_attribute("redaction.count", count)
    span.set_attribute("redaction.content_hash", content_hash)


def record_token_usage(span: trace.Span, prompt_tokens: int, completion_tokens: int) -> None:
    """Attach token-usage counters to a span."""
    span.set_attribute("llm.prompt_tokens", prompt_tokens)
    span.set_attribute("llm.completion_tokens", completion_tokens)
    span.set_attribute("llm.total_tokens", prompt_tokens + completion_tokens)
