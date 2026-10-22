"""Uploading an RFP and extracting its requirements."""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from app import azure_clients, metrics
from app.agents import lifecycle, runner
from app.agents.untrusted import fence
from app.auth.access import OWNER_KEY
from app.auth.entra import UserClaims, get_current_user
from app.auth.ratelimit import rate_limited
from app.config import Settings, get_settings
from app.ingestion.pipeline import extract_requirements, redacted_document_text
from app.ingestion.rfp_extract import (
    Requirement,
    RFPExtractionResult,
    add_questions,
    assign_ids,
    requirement_type,
)
from app.schemas import ExtractRequest
from app.services.drafts import load_rfp
from app.services.uploads import read_upload, validated_upload_filename

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/rfp", tags=["rfp"])


@router.post("/upload", dependencies=[Depends(rate_limited)])
def rfp_upload(
    file: UploadFile = File(...),
    user: UserClaims = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    """Upload an RFP document to Azure Blob Storage. The uploader becomes its owner."""
    filename = validated_upload_filename(file, (".pdf", ".docx", ".xlsx", ".xls", ".csv"))

    rfp_id = str(uuid.uuid4())[:12]
    blob_name = f"{rfp_id}/{filename}"
    container = azure_clients.container(settings, settings.storage_rfp_container)

    data = read_upload(file, settings)
    container.upload_blob(name=blob_name, data=data, overwrite=True, metadata={OWNER_KEY: user.oid})

    metrics.bump("rfp_uploads")
    logger.info("Uploaded RFP %s (%d bytes) by user %s", blob_name, len(data), user.oid)

    return {"rfp_id": rfp_id, "blob_name": blob_name, "filename": filename, "size_bytes": len(data)}


@router.post("/extract")
def rfp_extract(
    req: ExtractRequest,
    user: UserClaims = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    """Extract sections, requirements, questions from an uploaded RFP."""
    rfp = load_rfp(settings, user, req.rfp_id, req.filename)
    result = extract_requirements(rfp.filename, rfp.data)
    metrics.bump("extractions")
    return result.model_dump()


@router.post("/extract-ai", dependencies=[Depends(rate_limited)])
def rfp_extract_ai(
    req: ExtractRequest,
    user: UserClaims = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    """Extract requirements using the AI agent — better for unstructured documents."""
    rfp = load_rfp(settings, user, req.rfp_id, req.filename)

    # Redacted before it is sent to the model; truncated to fit the context window
    doc_text = redacted_document_text(rfp.filename, rfp.data)[:12000]

    agent_id = lifecycle.ensure_agent(lifecycle.DRAFT_AGENT_NAME)
    agent_result = runner.run_agent_prompt(
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
            f"Document content:\n\n{fence('RFP-DOCUMENT', doc_text)}"
        ),
    )

    if not agent_result.completed:
        raise HTTPException(status_code=502, detail=f"AI extraction failed: {agent_result.status}")

    safe_text, _ = runner.post_generation_guard(agent_result.text)
    try:
        parsed = runner.parse_json_object(safe_text)
    except (ValueError, IndexError):
        raise HTTPException(status_code=502, detail="AI returned invalid JSON for extraction") from None

    def listed(key: str) -> list:
        value = parsed.get(key)
        return value if isinstance(value, list) else []

    requirements = [
        Requirement(
            id=str(r.get("id", "")),
            text=str(r.get("text", "")),
            type=requirement_type(str(r.get("text", ""))),
            category=str(r.get("category", "general")),
            priority=str(r.get("priority", "standard")),
            source_section=str(r.get("source_section", "")),
            keywords=[],
        )
        for r in listed("requirements")
        if isinstance(r, dict)
    ]
    # Questions the model listed separately are requirements too (unless it also listed them as such)
    requirements = add_questions(requirements, [str(q) for q in listed("questions")])
    assign_ids(requirements)
    result = RFPExtractionResult(
        sections=[{"heading": "AI Extraction", "body": f"AI extracted {len(requirements)} requirements"}],
        requirements=requirements,
        questions=[r.text for r in requirements if r.type == "question"],
        evaluation_criteria=[str(c) for c in listed("evaluation_criteria")],
    )

    metrics.bump("extractions")
    return result.model_dump()
