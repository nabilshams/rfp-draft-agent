"""OpenTelemetry + Application Insights setup: exporter and FastAPI instrumentation.

The spans the pipeline adds (stages, tokens, cost) and the rule that no content may appear in them are in
:mod:`app.telemetry.spans`.
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
