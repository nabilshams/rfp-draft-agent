"""FastAPI application – RFP Draft Agent.

The endpoints live in :mod:`app.routers`:

  ops       GET /health, GET /metrics, WS /ws/logs
  rfp       POST /rfp/upload, /rfp/extract, /rfp/extract-ai
  draft     POST /draft, /draft/stream, /draft/generate-section, /draft/generate-doc, /answer
  kb        POST /kb/upload, /kb/ingest, /kb/reindex; GET /kb/list; DELETE /kb/delete
  outputs   GET /outputs/{rfp_id}/{filename}
  template  POST /template/upload, GET /template/info
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import logging_setup
from app.auth.entra import log_auth_mode
from app.config import get_settings
from app.routers import draft, kb, ops, outputs, rfp, template
from app.routers import jobs as jobs_router
from app.services import jobs

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown hooks."""
    from app.agents.lifecycle import setup_agents
    from app.telemetry.tracing import configure_tracing

    logging_setup.set_main_loop(asyncio.get_running_loop())
    logging_setup.install_ws_log_handler()
    settings = get_settings()
    logging_setup.configure_logging(settings.log_level)
    log_auth_mode(settings)
    configure_tracing(app)

    # Pre-create persistent agents on startup
    try:
        setup_agents()
    except Exception as exc:
        logger.warning("Agent setup deferred – will retry on first request: %s", exc)

    logger.info("RFP Draft Agent started")
    yield
    logger.info("RFP Draft Agent shutting down")
    jobs.shutdown()
    logging_setup.set_main_loop(None)


app = FastAPI(
    title="RFP Draft Agent",
    version="0.1.0",
    description="Azure AI Foundry code-first agent for RFP response drafting",
    lifespan=lifespan,
)

for module in (ops, rfp, draft, jobs_router, kb, outputs, template):
    app.include_router(module.router)

# The UI only runs scripts served from /static (it has no inline handlers), so an injected script
# can't run even if some untrusted text slipped into the page unescaped.
_UI_CSP = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data:",
        "connect-src 'self' ws: wss:",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ]
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    path = request.url.path
    if path == "/" or path.startswith("/static/"):
        response.headers["Content-Security-Policy"] = _UI_CSP
        response.headers.setdefault("X-Frame-Options", "DENY")
    return response

# ── Static files + UI ────────────────────────────────────
_static_dir = Path(__file__).parent / "static"
if _static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


@app.get("/", include_in_schema=False)
async def root():
    """Serve the web UI."""
    index = _static_dir / "index.html"
    if index.exists():
        return FileResponse(str(index))
    return RedirectResponse("/docs")


def run() -> None:
    """Run uvicorn using configuration from environment variables.

    Useful for local/dev runs where worker parallelism should be controlled
    via PARALLELISM in .env without repeating CLI flags.
    """
    import uvicorn

    settings = get_settings()
    workers = settings.parallelism
    if settings.uvicorn_reload and workers > 1:
        logger.warning("UVICORN_RELOAD=true disables multi-worker mode; forcing workers=1")
        workers = 1

    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.uvicorn_reload,
        workers=workers,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    run()
