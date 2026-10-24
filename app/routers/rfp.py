"""Uploading an RFP and extracting its requirements."""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, File, UploadFile

from app import azure_clients, metrics
from app.auth.access import OWNER_KEY
from app.auth.entra import UserClaims, get_current_user
from app.auth.ratelimit import rate_limited
from app.config import Settings, get_settings
from app.ingestion.pipeline import extract_requirements
from app.schemas import ExtractRequest
from app.services.drafts import load_rfp
from app.services.extraction import extract_with_model
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
    """Read the RFP with the model, every part of it (see :mod:`app.ingestion.ai_extract`): typed items, each one
    found in the document, and its dates and submission rules. Better than the patterns for documents written in prose."""
    rfp = load_rfp(settings, user, req.rfp_id, req.filename)
    result = extract_with_model(rfp.filename, rfp.data, settings)
    metrics.bump("extractions")
    return result.model_dump()
