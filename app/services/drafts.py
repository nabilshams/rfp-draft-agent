"""Steps shared by the RFP and draft endpoints: loading an RFP, choosing requirements, publishing outputs."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from azure.core.exceptions import ResourceNotFoundError
from fastapi import HTTPException

from app import azure_clients
from app.auth.access import require_rfp_access
from app.auth.entra import UserClaims
from app.config import Settings
from app.ingestion.pipeline import extract_requirements
from app.ingestion.rfp_extract import guidance_text

logger = logging.getLogger(__name__)


@dataclass
class RfpFile:
    filename: str
    data: bytes


def load_rfp(settings: Settings, user: UserClaims, rfp_id: str, filename: str | None = None) -> RfpFile:
    """Download an RFP the user is allowed to use (404 if it doesn't exist or belongs to someone else).

    Without *filename*, the RFP's single uploaded file is used.
    """
    container = azure_clients.container(settings, settings.storage_rfp_container)
    blob = require_rfp_access(container, rfp_id, user, settings)
    blob_name = f"{rfp_id}/{filename}" if filename else blob.name
    try:
        data = container.download_blob(blob_name).readall()
    except ResourceNotFoundError:
        raise HTTPException(status_code=404, detail=f"RFP blob not found: {blob_name}") from None
    return RfpFile(filename=blob_name.rsplit("/", 1)[-1], data=data)


@dataclass
class DraftInputs:
    requirements: list[dict[str, Any]]
    # The customer's evaluation criteria and response instructions, from the RFP file itself (redacted).
    guidance: str = ""


def prepare_draft(requested: list[dict[str, Any]], rfp: RfpFile) -> DraftInputs:
    """What a draft works from: the requirements to answer, and the RFP's own guidance on how to answer.

    The requirements are those the client sends (keeping the IDs it showed the user), else extracted.
    The guidance is always taken from the file, never from the request.
    """
    try:
        extraction = extract_requirements(rfp.filename, rfp.data)
    except Exception:
        if not requested:
            raise
        logger.exception("Could not read guidance from %s; drafting without it", rfp.filename)
        return DraftInputs(requirements=requested)
    requirements = requested or [r.model_dump() for r in extraction.requirements]
    return DraftInputs(requirements=requirements, guidance=guidance_text(extraction))


def web_search_choice(requested: bool, settings: Settings) -> bool:
    """Whether to attach web grounding; asking for it when it isn't configured is a client error."""
    if requested and not settings.bing_connection_id:
        raise HTTPException(status_code=400, detail="Web search is not configured on this deployment.")
    return requested


def publish_outputs(settings: Settings, rfp_id: str, docx_bytes: bytes, xlsx_bytes: bytes) -> dict[str, str]:
    """Store the generated documents and return their URLs."""
    container = azure_clients.container(settings, settings.storage_output_container)
    try:
        container.create_container()
    except Exception:
        pass  # already exists

    try:
        container.upload_blob(f"{rfp_id}/draft.docx", docx_bytes, overwrite=True)
        container.upload_blob(f"{rfp_id}/compliance.xlsx", xlsx_bytes, overwrite=True)
    except Exception as exc:
        logger.exception("Failed to upload generated outputs for rfp_id=%s", rfp_id)
        raise HTTPException(
            status_code=502,
            detail="Failed to upload generated documents to Azure Blob Storage. Check storage RBAC/credentials.",
        ) from exc

    base_url = f"{settings.azure_storage_account_url}/{settings.storage_output_container}"
    return {"docx_url": f"{base_url}/{rfp_id}/draft.docx", "xlsx_url": f"{base_url}/{rfp_id}/compliance.xlsx"}
