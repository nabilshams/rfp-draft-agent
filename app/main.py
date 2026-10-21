"""FastAPI application – RFP Draft Agent.

Endpoints:
  POST /rfp/upload    – upload an RFP document (PDF/DOCX)
  POST /rfp/extract   – extract structured requirements from an uploaded RFP
  POST /draft         – generate a full RFP response draft (DOCX + XLSX)
  POST /answer        – answer a single question with grounded citations
  GET  /health        – liveness probe
  GET  /metrics       – basic operational metrics
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import threading
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from app.auth.access import (
    OWNER_KEY,
    KbAcl,
    can_manage,
    can_view_kb,
    kb_metadata,
    parse_group_list,
    parse_kb_acl,
    require_admin,
    require_rfp_access,
    resolve_share_groups,
)
from app.auth.entra import UserClaims, authenticate, get_current_user, log_auth_mode
from app.auth.ratelimit import rate_limited
from app.config import Settings, get_settings
from app.validation import OUTPUT_FILENAMES, Filename, KbBlobName, RfpId, Segment, check_filename

logger = logging.getLogger(__name__)

_CONSOLE_HANDLER_NAME = "rfp-console"
_TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "templates" / "rfp_response_template.docx"

# ── Counters for /metrics ────────────────────────────────
_counters: dict[str, int] = {
    "rfp_uploads": 0,
    "extractions": 0,
    "drafts": 0,
    "answers": 0,
    "redaction_blocks": 0,
}
# Handlers run in worker threads, so increments must be serialised.
_counters_lock = threading.Lock()


def _bump(name: str) -> None:
    with _counters_lock:
        _counters[name] += 1


# ── WebSocket log broadcasting ───────────────────────────
_ws_clients: set[WebSocket] = set()
_log_buffer: deque[dict] = deque(maxlen=200)  # keep last 200 log entries
_main_loop: asyncio.AbstractEventLoop | None = None  # set at startup; log records may come from any thread
_ws_send_tasks: set[asyncio.Task] = set()  # keep references so in-flight sends aren't garbage collected


async def _send_log(ws: WebSocket, entry: dict) -> None:
    try:
        await ws.send_json(entry)
    except Exception:
        _ws_clients.discard(ws)


def _broadcast(entry: dict) -> None:
    """Runs on the event loop: start a send to every connected client."""
    for ws in list(_ws_clients):
        task = asyncio.get_running_loop().create_task(_send_log(ws, entry))
        _ws_send_tasks.add(task)
        task.add_done_callback(_ws_send_tasks.discard)


class WebSocketLogHandler(logging.Handler):
    """Logging handler that broadcasts log records to connected WebSocket clients."""

    def emit(self, record: logging.LogRecord) -> None:
        entry = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": self.format(record),
        }
        _log_buffer.append(entry)

        loop = _main_loop
        if loop is None or loop.is_closed() or not _ws_clients:
            return
        try:
            loop.call_soon_threadsafe(_broadcast, entry)
        except RuntimeError:
            pass  # loop is shutting down


def _configure_logging(level_name: str) -> None:
    """Send application logs to the console (container log stream) at the configured level.

    The root logger has no console handler otherwise, so records from ``app.*`` loggers would
    never leave the process except through the WebSocket handler.
    """
    root = logging.getLogger()
    if not any(h.get_name() == _CONSOLE_HANDLER_NAME for h in root.handlers):
        handler = logging.StreamHandler()
        handler.set_name(_CONSOLE_HANDLER_NAME)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s - %(message)s"))
        root.addHandler(handler)
    root.setLevel(getattr(logging, level_name.upper(), logging.INFO))
    # These log every outgoing HTTP request at INFO.
    for noisy in ("azure", "httpx", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _install_ws_log_handler() -> None:
    """Install the WebSocket handler on the root logger."""
    handler = WebSocketLogHandler()
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("%(name)s - %(message)s"))
    root = logging.getLogger()
    # Avoid duplicates on reload
    if not any(isinstance(h, WebSocketLogHandler) for h in root.handlers):
        root.addHandler(handler)


# ── Lifespan ─────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown hooks."""
    global _main_loop
    from app.telemetry.tracing import configure_tracing

    _main_loop = asyncio.get_running_loop()
    _install_ws_log_handler()
    _configure_logging(get_settings().log_level)
    log_auth_mode(get_settings())
    configure_tracing(app)

    # Pre-create persistent agents on startup
    from app.agents.foundry_agent import setup_agents
    try:
        setup_agents()
    except Exception as exc:
        logger.warning("Agent setup deferred – will retry on first request: %s", exc)

    logger.info("RFP Draft Agent started")
    yield
    logger.info("RFP Draft Agent shutting down")
    _main_loop = None


app = FastAPI(
    title="RFP Draft Agent",
    version="0.1.0",
    description="Azure AI Foundry code-first agent for RFP response drafting",
    lifespan=lifespan,
)

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


@app.websocket("/ws/logs")
async def ws_logs(websocket: WebSocket, settings: Settings = Depends(get_settings)):
    """Stream live log entries to the browser over WebSocket (authenticated like the HTTP API)."""
    try:
        await authenticate(websocket.headers, settings)
    except HTTPException:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    await websocket.accept()
    _ws_clients.add(websocket)
    logger.info("WebSocket log client connected (%d total)", len(_ws_clients))

    # Send recent log buffer to the new client
    for entry in _log_buffer:
        try:
            await websocket.send_json(entry)
        except Exception:
            break

    try:
        while True:
            # Keep connection alive; client doesn't send data
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        _ws_clients.discard(websocket)
        logger.info("WebSocket log client disconnected (%d remaining)", len(_ws_clients))


# ── Request / Response models ────────────────────────────


class ExtractRequest(BaseModel):
    rfp_id: RfpId
    filename: Filename


class DraftRequest(BaseModel):
    rfp_id: RfpId
    client_id: Segment
    project_id: Segment
    solution_area: Segment | None = None
    tone: str = "professional"
    approach: str = "per_requirement"  # "per_requirement" or "per_section" or "map_reduce"
    context: str = ""  # common context applied to every requirement
    requirements: list[dict[str, Any]] = Field(default_factory=list)


class AnswerRequest(BaseModel):
    question: str
    client_id: Segment
    project_id: Segment
    solution_area: Segment | None = None


class IngestRequest(BaseModel):
    """Re-ingest a KB document you own. Access comes from the blob's recorded owner and shared groups."""

    model_config = ConfigDict(extra="forbid")

    blob_name: KbBlobName
    redaction_level: str = "standard"


class GenerateDocRequest(BaseModel):
    rfp_id: RfpId
    responses: list[dict[str, Any]]
    section_narratives: list[dict[str, Any]] = []
    approach: str = "per_requirement"
    client_id: Segment = "demo-client"
    project_id: Segment = "demo-project"
    solution_area: Segment = "general"
    tone: str = "professional"


# ── Upload helpers ───────────────────────────────────────


def _validated_upload_filename(file: UploadFile, allowed_ext: tuple[str, ...]) -> str:
    if not file.filename:
        raise HTTPException(status_code=400, detail="Filename is required")
    try:
        check_filename(file.filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid file name: {exc}") from exc
    if not file.filename.lower().endswith(allowed_ext):
        raise HTTPException(status_code=400, detail=f"Only {allowed_ext} files are supported")
    return file.filename


def _read_upload(file: UploadFile, settings: Settings) -> bytes:
    """Read an uploaded file, rejecting anything over MAX_UPLOAD_MB."""
    limit = settings.max_upload_mb * 1024 * 1024
    data = file.file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(status_code=413, detail=f"File too large (maximum {settings.max_upload_mb} MB)")
    return data


# ── Storage and knowledge-base helpers ───────────────────


def _container(settings: Settings, name: str):
    from azure.identity import DefaultAzureCredential
    from azure.storage.blob import BlobServiceClient

    service = BlobServiceClient(
        account_url=settings.azure_storage_account_url,
        credential=DefaultAzureCredential(),
    )
    return service.get_container_client(name)


def _existing_kb_acl(container, blob_name: str) -> tuple[bool, KbAcl | None]:
    """(exists, recorded ACL) for a KB blob."""
    from azure.core.exceptions import ResourceNotFoundError

    try:
        props = container.get_blob_client(blob_name).get_blob_properties()
    except ResourceNotFoundError:
        return False, None
    return True, parse_kb_acl(props.metadata)


def _load_kb_document(container, blob_name: str, user: UserClaims, settings: Settings) -> KbAcl | None:
    """The ACL of a KB document the user is allowed to see.

    Missing documents and documents the user cannot see both give 404 so existence isn't revealed.
    """
    exists, acl = _existing_kb_acl(container, blob_name)
    if not exists or not can_view_kb(acl, user, settings):
        raise HTTPException(status_code=404, detail="Document not found")
    return acl


def _require_kb_manage(acl: KbAcl | None, blob_name: str, user: UserClaims, settings: Settings) -> None:
    if not can_manage(acl.owner if acl else None, user, settings):
        logger.warning("User %s denied changes to KB document %s", user.oid, blob_name)
        raise HTTPException(status_code=403, detail="Only the document's owner or an administrator can do this")


# ── Health / Metrics ─────────────────────────────────────


@app.get("/health", tags=["ops"])
async def health():
    return {"status": "healthy", "service": "rfp-draft-agent"}


@app.get("/metrics", tags=["ops"])
async def metrics(user: UserClaims = Depends(get_current_user)):
    return _counters


# ── RFP Upload ───────────────────────────────────────────


@app.post("/rfp/upload", tags=["rfp"], dependencies=[Depends(rate_limited)])
def rfp_upload(
    file: UploadFile = File(...),
    user: UserClaims = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    """Upload an RFP document to Azure Blob Storage. The uploader becomes its owner."""
    filename = _validated_upload_filename(file, (".pdf", ".docx", ".xlsx", ".xls", ".csv"))

    from azure.identity import DefaultAzureCredential
    from azure.storage.blob import BlobServiceClient

    rfp_id = str(uuid.uuid4())[:12]
    blob_name = f"{rfp_id}/{filename}"

    blob_service = BlobServiceClient(
        account_url=settings.azure_storage_account_url,
        credential=DefaultAzureCredential(),
    )
    container = blob_service.get_container_client(settings.storage_rfp_container)

    data = _read_upload(file, settings)
    container.upload_blob(name=blob_name, data=data, overwrite=True, metadata={OWNER_KEY: user.oid})

    _bump("rfp_uploads")
    logger.info("Uploaded RFP %s (%d bytes) by user %s", blob_name, len(data), user.oid)

    return {
        "rfp_id": rfp_id,
        "blob_name": blob_name,
        "filename": filename,
        "size_bytes": len(data),
    }


# ── RFP Extract ──────────────────────────────────────────


@app.post("/rfp/extract", tags=["rfp"])
def rfp_extract(
    req: ExtractRequest,
    user: UserClaims = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    """Extract sections, requirements, questions from an uploaded RFP."""
    from azure.identity import DefaultAzureCredential
    from azure.storage.blob import BlobServiceClient

    from app.ingestion.rfp_extract import extract_requirements_from_spreadsheet, extract_rfp_structure, extract_text

    blob_name = f"{req.rfp_id}/{req.filename}"
    blob_service = BlobServiceClient(
        account_url=settings.azure_storage_account_url,
        credential=DefaultAzureCredential(),
    )
    container = blob_service.get_container_client(settings.storage_rfp_container)
    require_rfp_access(container, req.rfp_id, user, settings)

    try:
        data = container.download_blob(blob_name).readall()
    except Exception:
        raise HTTPException(status_code=404, detail=f"RFP blob not found: {blob_name}")

    # Pre-extraction redaction
    from app.ingestion.redaction import get_redaction_engine
    engine = get_redaction_engine()

    # Use spreadsheet-aware extraction for xlsx/csv
    lower_name = req.filename.lower()
    if lower_name.endswith((".xlsx", ".xls", ".csv")):
        result = extract_requirements_from_spreadsheet(req.filename, data)
        # Redact requirement texts
        for r in result.requirements:
            r.text = engine.redact(r.text).sanitized_text
    else:
        raw_text = extract_text(req.filename, data)
        redaction_result = engine.redact(raw_text)
        sanitized_text = redaction_result.sanitized_text
        result = extract_rfp_structure(sanitized_text)
    _bump("extractions")

    return result.model_dump()


# ── AI-powered Extraction ─────────────────────────────────


@app.post("/rfp/extract-ai", tags=["rfp"], dependencies=[Depends(rate_limited)])
def rfp_extract_ai(
    req: ExtractRequest,
    user: UserClaims = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    """Extract requirements using AI agent — better for unstructured documents."""
    from azure.identity import DefaultAzureCredential
    from azure.storage.blob import BlobServiceClient

    from app.agents.foundry_agent import _DRAFT_AGENT_NAME, _ensure_agent, run_agent_prompt
    from app.ingestion.redaction import get_redaction_engine
    from app.ingestion.rfp_extract import (
        Requirement,
        RFPExtractionResult,
        _extract_text_from_csv,
        _extract_text_from_excel,
        extract_text,
    )

    blob_name = f"{req.rfp_id}/{req.filename}"
    blob_service = BlobServiceClient(
        account_url=settings.azure_storage_account_url,
        credential=DefaultAzureCredential(),
    )
    container = blob_service.get_container_client(settings.storage_rfp_container)
    require_rfp_access(container, req.rfp_id, user, settings)

    try:
        data = container.download_blob(blob_name).readall()
    except Exception:
        raise HTTPException(status_code=404, detail=f"RFP blob not found: {blob_name}")

    # Extract raw text from the document
    lower_name = req.filename.lower()
    if lower_name.endswith((".xlsx", ".xls")):
        raw_text = _extract_text_from_excel(data)
    elif lower_name.endswith(".csv"):
        raw_text = _extract_text_from_csv(data)
    else:
        raw_text = extract_text(req.filename, data)

    # Redact before sending to AI
    engine = get_redaction_engine()
    sanitized = engine.redact(raw_text).sanitized_text

    # Truncate to fit in context window
    doc_text = sanitized[:12000]

    # Use the agent to extract requirements
    agent_id = _ensure_agent(_DRAFT_AGENT_NAME)

    agent_result = run_agent_prompt(
        agent_id,
        (
            f"Extract ALL requirements, questions, and evaluation criteria from this RFP document.\n\n"
            f"Return ONLY a JSON object with:\n"
            f"- \"requirements\": array of objects, each with:\n"
            f"  - \"id\": a short identifier (e.g. R1, R2, F1, etc. — use the document's own IDs if present)\n"
            f"  - \"text\": the full requirement text\n"
            f"  - \"category\": one of: functional, technical, security, compliance, operational, general\n"
            f"  - \"priority\": mandatory, preferred, or standard\n"
            f"  - \"source_section\": which section of the document this came from\n"
            f"- \"questions\": array of strings (questions found in the RFP)\n"
            f"- \"evaluation_criteria\": array of strings\n\n"
            f"Extract EVERY requirement — look for shall/must/should statements, "
            f"numbered items, capability descriptions, compliance needs, and explicit questions.\n\n"
            f"Document content:\n\n{doc_text}"
        ),
    )

    if not agent_result.completed:
        raise HTTPException(status_code=502, detail=f"AI extraction failed: {agent_result.status}")

    # Parse the response
    raw_response = agent_result.text

    try:
        json_str = raw_response
        if "```json" in json_str:
            json_str = json_str.split("```json", 1)[1].split("```", 1)[0]
        elif "```" in json_str:
            json_str = json_str.split("```", 1)[1].split("```", 1)[0]
        parsed = json.loads(json_str.strip())
    except (json.JSONDecodeError, IndexError):
        raise HTTPException(status_code=502, detail="AI returned invalid JSON for extraction")

    # Convert to standard format
    requirements = []
    for r in parsed.get("requirements", []):
        requirements.append(Requirement(
            id=str(r.get("id", "")),
            text=r.get("text", ""),
            category=r.get("category", "general"),
            priority=r.get("priority", "standard"),
            source_section=r.get("source_section", ""),
            keywords=[],
        ))

    result = RFPExtractionResult(
        sections=[{"heading": "AI Extraction", "body": f"AI extracted {len(requirements)} requirements"}],
        requirements=requirements,
        questions=parsed.get("questions", []),
        evaluation_criteria=parsed.get("evaluation_criteria", []),
    )

    _bump("extractions")
    return result.model_dump()


# ── Draft ─────────────────────────────────────────────────


@app.post("/draft", tags=["agent"], dependencies=[Depends(rate_limited)])
def draft(
    req: DraftRequest,
    user: UserClaims = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    """Generate a full RFP response draft with DOCX + XLSX outputs."""
    # First, retrieve the extraction (caller should have run /rfp/extract first).
    # For the draft endpoint we expect extraction results stored locally or
    # re-extract.  Here we accept requirements in the request context via a
    # prior /rfp/extract call and cache key.  For MVP, the caller passes
    # rfp_id and we re-extract from blob.
    from azure.identity import DefaultAzureCredential
    from azure.storage.blob import BlobServiceClient

    from app.agents.foundry_agent import run_draft
    from app.outputs.docx_writer import generate_docx
    from app.outputs.xlsx_writer import generate_xlsx

    blob_service = BlobServiceClient(
        account_url=settings.azure_storage_account_url,
        credential=DefaultAzureCredential(),
    )

    # Find the RFP blob (404 if it doesn't exist or belongs to someone else)
    rfp_container = blob_service.get_container_client(settings.storage_rfp_container)
    blob_name = require_rfp_access(rfp_container, req.rfp_id, user, settings).name
    data = rfp_container.download_blob(blob_name).readall()
    filename = blob_name.rsplit("/", 1)[-1]

    from app.ingestion.redaction import get_redaction_engine
    from app.ingestion.rfp_extract import extract_requirements_from_spreadsheet, extract_rfp_structure, extract_text

    requirements: list[dict[str, Any]]
    if req.requirements:
        # Preserve IDs from the extraction shown in the UI so streamed updates map correctly.
        requirements = req.requirements
    else:
        engine = get_redaction_engine()
        lower_name = filename.lower()
        if lower_name.endswith((".xlsx", ".xls", ".csv")):
            extraction = extract_requirements_from_spreadsheet(filename, data)
            for r in extraction.requirements:
                r.text = engine.redact(r.text).sanitized_text
        else:
            raw_text = extract_text(filename, data)
            sanitized = engine.redact(raw_text).sanitized_text
            extraction = extract_rfp_structure(sanitized)
        requirements = [r.model_dump() for r in extraction.requirements]

    requirements_json = json.dumps(requirements, indent=2)

    # Run the agent
    start = time.time()
    draft_result = run_draft(
        rfp_id=req.rfp_id,
        requirements_json=requirements_json,
        client_id=req.client_id,
        project_id=req.project_id,
        solution_area=req.solution_area,
        tone=req.tone,
        user_oid=user.oid,
        user_groups=user.groups,
    )
    elapsed = time.time() - start
    logger.info("Draft generation took %.1fs for RFP %s", elapsed, req.rfp_id)

    if "error" in draft_result:
        raise HTTPException(status_code=502, detail=draft_result["error"])

    # Generate outputs
    docx_bytes = generate_docx(draft_result)
    responses_for_xlsx = draft_result.get("responses", [])
    xlsx_bytes = generate_xlsx(
        responses_for_xlsx,
        requirements=requirements,
    )

    # Upload outputs to blob
    output_container = blob_service.get_container_client(settings.storage_output_container)
    try:
        output_container.create_container()
    except Exception:
        pass  # already exists

    try:
        output_container.upload_blob(f"{req.rfp_id}/draft.docx", docx_bytes, overwrite=True)
        output_container.upload_blob(f"{req.rfp_id}/compliance.xlsx", xlsx_bytes, overwrite=True)
    except Exception as exc:
        logger.exception("Failed to upload generated outputs for rfp_id=%s", req.rfp_id)
        raise HTTPException(
            status_code=502,
            detail="Failed to upload generated documents to Azure Blob Storage. Check storage RBAC/credentials.",
        ) from exc

    _bump("drafts")

    base_url = f"{settings.azure_storage_account_url}/{settings.storage_output_container}"
    return {
        "rfp_id": req.rfp_id,
        "draft": draft_result,
        "outputs": {
            "docx_url": f"{base_url}/{req.rfp_id}/draft.docx",
            "xlsx_url": f"{base_url}/{req.rfp_id}/compliance.xlsx",
        },
        "elapsed_seconds": round(elapsed, 1),
    }


# ── Streaming Draft (SSE) ────────────────────────────────


@app.post("/draft/stream", tags=["agent"], dependencies=[Depends(rate_limited)])
def draft_stream(
    req: DraftRequest,
    user: UserClaims = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    """Stream RFP response generation – one SSE event per requirement."""
    from azure.identity import DefaultAzureCredential
    from azure.storage.blob import BlobServiceClient

    from app.agents.foundry_agent import stream_map_reduce, stream_per_requirement, stream_per_section
    from app.ingestion.redaction import get_redaction_engine
    from app.ingestion.rfp_extract import extract_requirements_from_spreadsheet, extract_rfp_structure, extract_text
    from app.outputs.docx_writer import parse_template_sections

    blob_service = BlobServiceClient(
        account_url=settings.azure_storage_account_url,
        credential=DefaultAzureCredential(),
    )
    rfp_container = blob_service.get_container_client(settings.storage_rfp_container)
    blob_name = require_rfp_access(rfp_container, req.rfp_id, user, settings).name
    data = rfp_container.download_blob(blob_name).readall()
    filename = blob_name.rsplit("/", 1)[-1]

    if req.requirements:
        # Use IDs from Step 2 extraction so UI cards update in real time.
        requirements = req.requirements
    else:
        engine = get_redaction_engine()
        lower_name = filename.lower()
        if lower_name.endswith((".xlsx", ".xls", ".csv")):
            extraction = extract_requirements_from_spreadsheet(filename, data)
            for r in extraction.requirements:
                r.text = engine.redact(r.text).sanitized_text
        else:
            raw_text = extract_text(filename, data)
            sanitized = engine.redact(raw_text).sanitized_text
            extraction = extract_rfp_structure(sanitized)
        requirements = [r.model_dump() for r in extraction.requirements]
    approach = req.approach  # "per_requirement" or "per_section"

    def event_generator():
        yield f"data: {json.dumps({'type': 'start', 'total': len(requirements), 'approach': approach})}\n\n"

        try:
            if approach == "map_reduce":
                # ── Approach 3: Map-Reduce ──
                template_sections = parse_template_sections()
                all_responses = []
                section_narratives = []

                for event in stream_map_reduce(
                    requirements=requirements,
                    template_sections=template_sections,
                    client_id=req.client_id,
                    project_id=req.project_id,
                    solution_area=req.solution_area,
                    tone=req.tone,
                    user_oid=user.oid,
                    user_groups=user.groups,
                    user_context=req.context,
                ):
                    event_type = event.get("type")
                    if event_type == "phase":
                        yield f"data: {json.dumps(event)}\n\n"
                    elif event_type == "response":
                        all_responses.append(event["data"])
                        yield f"data: {json.dumps(event)}\n\n"
                    elif event_type == "section_map":
                        yield f"data: {json.dumps(event)}\n\n"
                    elif event_type == "section_narrative":
                        section_narratives.append(event["data"])
                        yield f"data: {json.dumps(event)}\n\n"

                from app.outputs.docx_writer import generate_docx
                from app.outputs.xlsx_writer import generate_xlsx

                draft_result = {
                    "executive_summary": next(
                        (s["narrative_markdown"] for s in section_narratives
                         if "executive" in s.get("section_name", "").lower()),
                        "See section narratives.",
                    ),
                    "solution_overview": next(
                        (s["narrative_markdown"] for s in section_narratives
                         if "overview" in s.get("section_name", "").lower()
                         or "solution" in s.get("section_name", "").lower()),
                        "See section narratives.",
                    ),
                    "responses": all_responses,
                }
                docx_bytes = generate_docx(draft_result)
                xlsx_bytes = generate_xlsx(all_responses, requirements)

            elif approach == "per_section":
                # ── Approach 2: Section-based narrative generation ──
                template_sections = parse_template_sections()
                all_responses = []
                section_narratives = []

                for event in stream_per_section(
                    requirements=requirements,
                    template_sections=template_sections,
                    client_id=req.client_id,
                    project_id=req.project_id,
                    solution_area=req.solution_area,
                    tone=req.tone,
                    user_oid=user.oid,
                    user_groups=user.groups,
                    user_context=req.context,
                ):
                    event_type = event.get("type")
                    if event_type == "section_map":
                        yield f"data: {json.dumps(event)}\n\n"
                    elif event_type == "section_narrative":
                        section_narratives.append(event["data"])
                        yield f"data: {json.dumps(event)}\n\n"
                    elif event_type == "requirement_started":
                        yield f"data: {json.dumps(event)}\n\n"
                    elif event_type == "requirement_compliance":
                        rid = event.get("data", {}).get("requirement_id")
                        logger.info("[RFP-TRACE] SSE response(per_section) requirement_id=%s", rid)
                        all_responses.append(event["data"])
                        yield f"data: {json.dumps({'type': 'response', 'data': event['data']})}\n\n"

                # Build draft result for DOCX/XLSX
                from app.outputs.docx_writer import generate_docx
                from app.outputs.xlsx_writer import generate_xlsx

                draft_result = {
                    "executive_summary": next(
                        (s["narrative_markdown"] for s in section_narratives
                         if "executive" in s.get("section_name", "").lower()),
                        "See section narratives.",
                    ),
                    "solution_overview": next(
                        (s["narrative_markdown"] for s in section_narratives
                         if "overview" in s.get("section_name", "").lower()
                         or "solution" in s.get("section_name", "").lower()),
                        "See section narratives.",
                    ),
                    "responses": all_responses,
                }
                docx_bytes = generate_docx(draft_result)
                xlsx_bytes = generate_xlsx(all_responses, requirements)

            else:
                # ── Approach 1: Per-requirement (default) ──
                all_responses = []
                for resp in stream_per_requirement(
                    requirements=requirements,
                    client_id=req.client_id,
                    project_id=req.project_id,
                    solution_area=req.solution_area,
                    tone=req.tone,
                    user_oid=user.oid,
                    user_groups=user.groups,
                    user_context=req.context,
                ):
                    logger.info("[RFP-TRACE] SSE response(per_requirement) requirement_id=%s", resp.get("requirement_id"))
                    all_responses.append(resp)
                    yield f"data: {json.dumps({'type': 'response', 'data': resp})}\n\n"

                from app.outputs.docx_writer import generate_docx
                from app.outputs.xlsx_writer import generate_xlsx

                draft_result = {
                    "executive_summary": "See individual requirement responses.",
                    "solution_overview": "Responses generated per requirement.",
                    "responses": all_responses,
                }
                docx_bytes = generate_docx(draft_result)
                xlsx_bytes = generate_xlsx(all_responses, requirements)

            # Upload outputs
            output_container = blob_service.get_container_client(settings.storage_output_container)
            try:
                output_container.create_container()
            except Exception:
                pass
            output_container.upload_blob(f"{req.rfp_id}/draft.docx", docx_bytes, overwrite=True)
            output_container.upload_blob(f"{req.rfp_id}/compliance.xlsx", xlsx_bytes, overwrite=True)

            _bump("drafts")
            base_url = f"{settings.azure_storage_account_url}/{settings.storage_output_container}"

            yield f"data: {json.dumps({'type': 'complete', 'outputs': {'docx_url': f'{base_url}/{req.rfp_id}/draft.docx', 'xlsx_url': f'{base_url}/{req.rfp_id}/compliance.xlsx'}})}\n\n"
        except Exception as exc:
            logger.exception("Draft stream failed for rfp_id=%s", req.rfp_id)
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Generate Single Section ──────────────────────────────


class GenerateSectionRequest(BaseModel):
    section_name: str
    prompt: str
    responses: list[dict[str, Any]] = []
    tone: str = "professional"


@app.post("/draft/generate-section", tags=["agent"], dependencies=[Depends(rate_limited)])
def generate_section(
    req: GenerateSectionRequest,
    user: UserClaims = Depends(get_current_user),
):
    """Generate content for a single template section using the provided prompt and responses."""
    from app.agents.foundry_agent import _DRAFT_AGENT_NAME, _ensure_agent, _post_generation_guard, run_agent_prompt

    agent_id = _ensure_agent(_DRAFT_AGENT_NAME)

    # Build context from responses if available
    responses_context = ""
    if req.responses:
        responses_context = "\n\n--- Requirement Responses (use these as source material) ---\n\n"
        responses_context += "\n\n".join(
            f"### [{r.get('requirement_id', '?')}] (Compliance: {r.get('meets', '?')})\n"
            f"Requirement: {r.get('requirement_text', '')[:200]}\n"
            f"Response: {r.get('answer_markdown', '')}"
            for r in req.responses
        )

    full_prompt = req.prompt + responses_context + (
        f"\n\nReturn ONLY a JSON object:\n"
        f"{{\"section_name\": \"{req.section_name}\", "
        f"\"narrative_markdown\": \"<your section text>\"}}"
    )

    agent_result = run_agent_prompt(agent_id, full_prompt)

    if not agent_result.completed:
        raise HTTPException(status_code=502, detail=f"Section generation failed: {agent_result.status}")

    safe, _ = _post_generation_guard(agent_result.text)

    try:
        text = safe
        if "```json" in text:
            text = text.split("```json", 1)[1].split("```", 1)[0]
        elif "```" in text:
            text = text.split("```", 1)[1].split("```", 1)[0]
        result = json.loads(text.strip())
    except (json.JSONDecodeError, IndexError):
        result = {"section_name": req.section_name, "narrative_markdown": safe}

    return result


# ── Generate Document from Responses ─────────────────────


@app.post("/draft/generate-doc", tags=["agent"], dependencies=[Depends(rate_limited)])
def generate_doc(
    req: GenerateDocRequest,
    user: UserClaims = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    """Generate a final RFP response document from pre-generated requirement responses.

    Uses the template sections to organize responses into coherent narratives,
    then produces DOCX + XLSX outputs.
    """
    from azure.identity import DefaultAzureCredential
    from azure.storage.blob import BlobServiceClient

    from app.agents.foundry_agent import _DRAFT_AGENT_NAME, _ensure_agent, _post_generation_guard, run_agent_prompt
    from app.outputs.docx_writer import generate_docx, parse_template_sections
    from app.outputs.xlsx_writer import generate_xlsx

    responses = req.responses
    if not responses and not req.section_narratives:
        raise HTTPException(status_code=400, detail="No responses or section narratives provided")

    rfp_container = BlobServiceClient(
        account_url=settings.azure_storage_account_url,
        credential=DefaultAzureCredential(),
    ).get_container_client(settings.storage_rfp_container)
    require_rfp_access(rfp_container, req.rfp_id, user, settings)

    template_sections = parse_template_sections()
    section_names = [s["heading"] for s in template_sections] if template_sections else [
        "Executive Summary", "Solution Overview", "Technical Approach",
        "Compliance & Security", "Implementation & Support",
    ]

    agent_id = _ensure_agent(_DRAFT_AGENT_NAME)

    section_map = {}
    sections_result = []

    # If per_section was selected and section narratives are already available,
    # reuse them directly and skip re-generating section drafts.
    if req.approach == "per_section" and req.section_narratives:
        sections_result = req.section_narratives
        section_map = {
            s.get("section_name", f"Section {i + 1}"): s.get("requirement_ids", [])
            for i, s in enumerate(sections_result)
        }
    else:
        # Step 1: Classify responses into sections
        req_summaries = "\n".join(
            f"- {r.get('requirement_id', '?')}: {r.get('requirement_text', r.get('answer_markdown', ''))[:100]}"
            for r in responses
        )

        classify_result = run_agent_prompt(
            agent_id,
            (
                f"Classify each requirement into the best document section.\n\n"
                f"Sections: {json.dumps(section_names)}\n\n"
                f"Requirements:\n{req_summaries}\n\n"
                f"Return ONLY a JSON object mapping section names to arrays of requirement IDs.\n"
                f"Every requirement must appear in exactly one section."
            ),
        )

        if classify_result.completed:
            try:
                text = classify_result.text
                if "```json" in text:
                    text = text.split("```json", 1)[1].split("```", 1)[0]
                elif "```" in text:
                    text = text.split("```", 1)[1].split("```", 1)[0]
                section_map = json.loads(text.strip())
            except Exception:
                pass

        if not section_map:
            section_map = {section_names[0]: [r.get("requirement_id", "?") for r in responses]}

        # Step 2: Generate narrative per section
        resp_by_id = {r.get("requirement_id", ""): r for r in responses}

        for section_name, req_ids in section_map.items():
            section_answers = [resp_by_id[rid] for rid in req_ids if rid in resp_by_id]
            if not section_answers:
                continue

            answers_digest = "\n\n".join(
                f"### [{a.get('requirement_id', '?')}] (Compliance: {a.get('meets', '?')})\n"
                f"Requirement: {a.get('requirement_text', '')[:200]}\n"
                f"Response: {a.get('answer_markdown', '')}"
                for a in section_answers
            )

            narrative_result = run_agent_prompt(
                agent_id,
                (
                    f"Write the \"{section_name}\" section of a professional RFP response.\n"
                    f"Tone: {req.tone}.\n\n"
                    f"Synthesize these individual answers into a coherent narrative. "
                    f"Reference requirement IDs in brackets, e.g. [F1].\n"
                    f"Keep all factual claims from the answers. Do not add new information.\n\n"
                    f"--- Answers ---\n\n{answers_digest}\n\n"
                    f"Return ONLY a JSON object:\n"
                    f"{{\"section_name\": \"{section_name}\", "
                    f"\"narrative_markdown\": \"<text>\", "
                    f"\"requirement_ids\": {json.dumps(req_ids)}}}"
                ),
            )

            if narrative_result.completed:
                safe, _ = _post_generation_guard(narrative_result.text)
                try:
                    text = safe
                    if "```json" in text:
                        text = text.split("```json", 1)[1].split("```", 1)[0]
                    elif "```" in text:
                        text = text.split("```", 1)[1].split("```", 1)[0]
                    section_data = json.loads(text.strip())
                except Exception:
                    section_data = {"section_name": section_name, "narrative_markdown": safe, "requirement_ids": req_ids}
                sections_result.append(section_data)

    # Generate DOCX + XLSX
    draft_result = {
        "executive_summary": next(
            (s["narrative_markdown"] for s in sections_result if "executive" in s.get("section_name", "").lower()), "See sections.",
        ),
        "solution_overview": next(
            (s["narrative_markdown"] for s in sections_result if "overview" in s.get("section_name", "").lower() or "solution" in s.get("section_name", "").lower()), "See sections.",
        ),
        "section_narratives": sections_result,
        "responses": responses,
    }
    docx_bytes = generate_docx(draft_result)
    xlsx_bytes = generate_xlsx(responses, [])

    blob_service = BlobServiceClient(
        account_url=settings.azure_storage_account_url,
        credential=DefaultAzureCredential(),
    )
    output_container = blob_service.get_container_client(settings.storage_output_container)
    try:
        output_container.create_container()
    except Exception:
        pass
    try:
        output_container.upload_blob(f"{req.rfp_id}/draft.docx", docx_bytes, overwrite=True)
        output_container.upload_blob(f"{req.rfp_id}/compliance.xlsx", xlsx_bytes, overwrite=True)
    except Exception as exc:
        logger.exception("Failed to upload generated outputs for rfp_id=%s", req.rfp_id)
        raise HTTPException(
            status_code=502,
            detail="Failed to upload generated documents to Azure Blob Storage. Check storage RBAC/credentials.",
        ) from exc

    base_url = f"{settings.azure_storage_account_url}/{settings.storage_output_container}"
    return {
        "sections": sections_result,
        "section_map": section_map,
        "outputs": {
            "docx_url": f"{base_url}/{req.rfp_id}/draft.docx",
            "xlsx_url": f"{base_url}/{req.rfp_id}/compliance.xlsx",
        },
    }


# ── Single Answer ─────────────────────────────────────────


@app.post("/answer", tags=["agent"], dependencies=[Depends(rate_limited)])
def answer(
    req: AnswerRequest,
    user: UserClaims = Depends(get_current_user),
):
    """Answer a single question using the grounded agent."""
    from app.agents.foundry_agent import run_single_answer
    from app.ingestion.redaction import get_redaction_engine

    # Pre-redact the question
    engine = get_redaction_engine()
    redacted_q = engine.redact(req.question)

    result = run_single_answer(
        question=redacted_q.sanitized_text,
        client_id=req.client_id,
        project_id=req.project_id,
        solution_area=req.solution_area,
        user_oid=user.oid,
        user_groups=user.groups,
    )

    _bump("answers")
    return result


# ── KB Ingest ─────────────────────────────────────────────


@app.post("/kb/ingest", tags=["knowledge-base"], dependencies=[Depends(rate_limited)])
def kb_ingest(
    req: IngestRequest,
    user: UserClaims = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    """Re-ingest a KB document from Blob → AI Search. Only its owner (or an administrator) can.

    Who can retrieve the indexed chunks comes from the blob's recorded owner and shared groups,
    never from the request. An administrator ingesting a document that has no recorded owner
    becomes its owner.
    """
    from app.ingestion.kb_ingest import ingest_blob

    container = _container(settings, settings.storage_kb_container)
    acl = _load_kb_document(container, req.blob_name, user, settings)
    _require_kb_manage(acl, req.blob_name, user, settings)
    if acl is None:
        acl = KbAcl(owner=user.oid)
        container.get_blob_client(req.blob_name).set_blob_metadata(kb_metadata(acl.owner, acl.groups))

    client_id, project_id, solution_area, _ = req.blob_name.split("/")
    count = ingest_blob(
        blob_name=req.blob_name,
        client_id=client_id,
        project_id=project_id,
        solution_area=solution_area,
        oids=[acl.owner],
        groups=list(acl.groups),
        redaction_level=req.redaction_level,
    )
    return {"indexed_chunks": count, "blob_name": req.blob_name}


# ── Download outputs ──────────────────────────────────────


@app.get("/outputs/{rfp_id}/{filename}", tags=["outputs"])
def download_output(
    rfp_id: RfpId,
    filename: Filename,
    user: UserClaims = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    """Download a generated output file (draft.docx or compliance.xlsx) of an RFP you own."""
    if filename not in OUTPUT_FILENAMES:
        raise HTTPException(status_code=404, detail=f"Output not found: {rfp_id}/{filename}")
    require_rfp_access(_container(settings, settings.storage_rfp_container), rfp_id, user, settings)

    container = _container(settings, settings.storage_output_container)
    blob_name = f"{rfp_id}/{filename}"

    try:
        data = container.download_blob(blob_name).readall()
    except Exception:
        raise HTTPException(status_code=404, detail=f"Output not found: {blob_name}")

    media_type = (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        if filename.endswith(".docx")
        else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )

    return StreamingResponse(
        io.BytesIO(data),
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── KB Upload + Ingest (combined) ─────────────────────────


@app.post("/kb/upload", tags=["knowledge-base"], dependencies=[Depends(rate_limited)])
def kb_upload(
    file: UploadFile = File(...),
    client_id: Annotated[Segment, Form()] = "demo-client",
    project_id: Annotated[Segment, Form()] = "demo-project",
    solution_area: Annotated[Segment, Form()] = "general",
    share_with_groups: Annotated[str, Form()] = "",
    user: UserClaims = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    """Upload a reference document, store in Blob, and ingest into the search index.

    The document is private to the uploader unless ``share_with_groups`` (comma-separated group IDs
    the uploader belongs to) is given. Uploading again replaces the document and its sharing; only
    the owner (or an administrator) can replace an existing document.
    """
    filename = _validated_upload_filename(file, (".pdf", ".docx"))
    groups = resolve_share_groups(parse_group_list(share_with_groups), user, settings)

    from app.ingestion.kb_ingest import ingest_blob

    blob_name = f"{client_id}/{project_id}/{solution_area}/{filename}"
    container = _container(settings, settings.storage_kb_container)

    exists, existing_acl = _existing_kb_acl(container, blob_name)
    if exists:
        _require_kb_manage(existing_acl, blob_name, user, settings)
    owner = existing_acl.owner if existing_acl else user.oid  # an admin replacing a file keeps its owner

    data = _read_upload(file, settings)
    container.upload_blob(name=blob_name, data=data, overwrite=True, metadata=kb_metadata(owner, groups))
    logger.info("Uploaded KB doc %s (%d bytes)", blob_name, len(data))

    # Ingest into search index
    count = ingest_blob(
        blob_name=blob_name,
        client_id=client_id,
        project_id=project_id,
        solution_area=solution_area,
        oids=[owner],
        groups=groups,
    )

    return {
        "blob_name": blob_name,
        "filename": filename,
        "size_bytes": len(data),
        "indexed_chunks": count,
        "client_id": client_id,
        "project_id": project_id,
        "solution_area": solution_area,
        "shared_with_groups": groups,
    }


@app.get("/kb/list", tags=["knowledge-base"])
def kb_list(
    user: UserClaims = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    """List the knowledge-base documents you own or that are shared with one of your groups."""
    container = _container(settings, settings.storage_kb_container)

    docs = []
    try:
        for blob in container.list_blobs(include=["metadata"]):
            acl = parse_kb_acl(blob.metadata)
            if not can_view_kb(acl, user, settings):
                continue
            parts = blob.name.split("/")
            docs.append({
                "blob_name": blob.name,
                "filename": parts[-1] if parts else blob.name,
                "client_id": parts[0] if len(parts) > 1 else "",
                "project_id": parts[1] if len(parts) > 2 else "",
                "solution_area": parts[2] if len(parts) > 3 else "",
                "size_bytes": blob.size,
                "last_modified": blob.last_modified.isoformat() if blob.last_modified else "",
                "owned_by_me": acl is not None and acl.owner == user.oid,
                "can_delete": can_manage(acl.owner if acl else None, user, settings),
                "shared_with_groups": list(acl.groups) if acl else [],
            })
    except Exception:
        pass

    return {"documents": docs, "count": len(docs)}


@app.delete("/kb/delete", tags=["knowledge-base"])
def kb_delete(
    blob_name: Annotated[KbBlobName, Query()],
    user: UserClaims = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    """Delete a KB document from Blob Storage and remove its chunks from the search index.

    Only the document's owner (or an administrator) can delete it.
    """
    import hashlib

    from azure.identity import DefaultAzureCredential
    from azure.search.documents import SearchClient

    from app.agents.retrieval import escape_odata_literal

    credential = DefaultAzureCredential()

    container = _container(settings, settings.storage_kb_container)
    _require_kb_manage(_load_kb_document(container, blob_name, user, settings), blob_name, user, settings)

    # Delete from blob
    try:
        container.delete_blob(blob_name)
        logger.info("Deleted blob: %s", blob_name)
    except Exception as exc:
        logger.warning("Could not delete blob %s: %s", blob_name, exc)

    # Delete chunks from the search index (chunks have IDs based on blob_name)
    search_endpoint = os.environ.get(
        "AZURE_AI_SEARCH_ENDPOINT",
        f"https://{settings.azure_ai_search_connection_name}.search.windows.net",
    )
    search_client = SearchClient(
        endpoint=search_endpoint,
        index_name=settings.ai_search_index_name,
        credential=credential,
    )

    # Find and delete all chunks that came from this blob
    deleted_chunks = 0
    try:
        # Search for documents with matching source_url
        source_url = f"{settings.azure_storage_account_url}/{settings.storage_kb_container}/{blob_name}"
        results = search_client.search(
            search_text="*",
            filter=f"source_url eq '{escape_odata_literal(source_url)}'",
            select=["id"],
            top=1000,
        )
        doc_ids = [{"id": r["id"]} for r in results]
        if doc_ids:
            search_client.delete_documents(documents=doc_ids)
            deleted_chunks = len(doc_ids)
            logger.info("Deleted %d chunks for %s", deleted_chunks, blob_name)
    except Exception as exc:
        # Fallback: try hash-based IDs
        logger.warning("Filter-based delete failed (%s), trying hash-based", exc)
        try:
            doc_ids = []
            for i in range(200):  # max 200 chunks per doc
                doc_id = hashlib.md5(f"{blob_name}:{i}".encode()).hexdigest()
                doc_ids.append({"id": doc_id})
            search_client.delete_documents(documents=doc_ids)
            deleted_chunks = len(doc_ids)
        except Exception:
            pass

    return {"blob_name": blob_name, "deleted_chunks": deleted_chunks}


@app.post("/kb/reindex", tags=["knowledge-base"], dependencies=[Depends(rate_limited)])
def kb_reindex(
    user: UserClaims = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    """Re-index every KB document (administrators only).

    Each document keeps the access recorded on its blob (owner and shared groups). Documents with no
    recorded owner are skipped: ingest one explicitly with POST /kb/ingest to claim it.
    """
    require_admin(user, settings)

    from app.ingestion.kb_ingest import ingest_blob

    container = _container(settings, settings.storage_kb_container)

    results = []
    for blob in container.list_blobs(include=["metadata"]):
        acl = parse_kb_acl(blob.metadata)
        parts = blob.name.split("/")
        if acl is None or len(parts) != 4:
            reason = "no recorded owner" if acl is None else "unexpected blob name"
            results.append({"blob_name": blob.name, "chunks": 0, "status": f"skipped: {reason}"})
            continue
        try:
            count = ingest_blob(
                blob_name=blob.name,
                client_id=parts[0],
                project_id=parts[1],
                solution_area=parts[2],
                oids=[acl.owner],
                groups=list(acl.groups),
            )
            results.append({"blob_name": blob.name, "chunks": count, "status": "ok"})
        except Exception as exc:
            results.append({"blob_name": blob.name, "chunks": 0, "status": f"error: {exc}"})

    total_chunks = sum(r["chunks"] for r in results)
    logger.info("Re-indexed %d documents, %d total chunks", len(results), total_chunks)
    return {"documents": len(results), "total_chunks": total_chunks, "details": results}


# ── Template Upload ───────────────────────────────────────


@app.post("/template/upload", tags=["template"], dependencies=[Depends(rate_limited)])
def template_upload(
    file: UploadFile = File(...),
    user: UserClaims = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    """Upload the DOCX template used for every RFP response (administrators only: it is shared)."""
    require_admin(user, settings)
    filename = _validated_upload_filename(file, (".docx", ".doc"))

    template_path = _TEMPLATE_PATH
    template_path.parent.mkdir(parents=True, exist_ok=True)

    data = _read_upload(file, settings)

    # Validate the file is actually a valid DOCX (ZIP-based)
    format_warning = None
    if data[:2] != b"PK":
        if data[:8] == bytes.fromhex("D0CF11E0A1B11AE1"):
            format_warning = (
                "This file is in the old .doc (OLE2) format, not .docx. "
                "Please re-save it in Word as 'Word Document (.docx)' format "
                "(File → Save As → choose 'Word Document (.docx)'). "
                "The template has been saved but section detection will not work "
                "until it is in .docx format."
            )
        else:
            format_warning = "This file does not appear to be a valid Word document."

    template_path.write_bytes(data)
    logger.info("Template uploaded: %s (%d bytes) → %s", filename, len(data), template_path)

    # Parse sections from the uploaded template
    from app.outputs.docx_writer import parse_template_sections
    sections = parse_template_sections(template_path)

    result = {
        "filename": filename,
        "size_bytes": len(data),
        "sections": sections,
        "message": "Template saved. It will be used for all future draft generations.",
    }
    if format_warning:
        result["warning"] = format_warning
    return result


@app.get("/template/info", tags=["template"])
def template_info():
    """Check whether a custom template is loaded and return its sections."""
    from app.outputs.docx_writer import parse_template_sections

    template_path = _TEMPLATE_PATH
    if template_path.exists():
        stat = template_path.stat()
        sections = parse_template_sections(template_path)
        return {
            "exists": True,
            "size_bytes": stat.st_size,
            "last_modified": datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
            "sections": sections,
        }
    return {"exists": False, "sections": []}


def _env_flag(name: str, default: bool = False) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def run() -> None:
    """Run uvicorn using configuration from environment variables.

    Useful for local/dev runs where worker parallelism should be controlled
    via PARALLELISM in .env without repeating CLI flags.
    """
    import uvicorn

    settings = get_settings()
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    log_level = os.environ.get("LOG_LEVEL", "info").lower()
    reload_enabled = _env_flag("UVICORN_RELOAD", default=False)
    workers = settings.parallelism

    if reload_enabled and workers > 1:
        logger.warning("UVICORN_RELOAD=true disables multi-worker mode; forcing workers=1")
        workers = 1

    uvicorn.run(
        "app.main:app",
        host=host,
        port=port,
        reload=reload_enabled,
        workers=workers,
        log_level=log_level,
    )


if __name__ == "__main__":
    run()
