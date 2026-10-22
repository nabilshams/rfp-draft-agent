"""Downloading generated documents."""

from __future__ import annotations

import io
import re

from azure.core.exceptions import ResourceNotFoundError
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from app import azure_clients
from app.auth.access import require_rfp_access
from app.auth.entra import UserClaims, get_current_user
from app.config import Settings, get_settings
from app.ledger.service import PROJECT_ID_PATTERN, can_view
from app.ledger.store import store_for
from app.validation import OUTPUT_FILENAMES, Filename, RfpId

router = APIRouter(tags=["outputs"])

_MEDIA_TYPES = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def _authorise(rfp_id: str, user: UserClaims, settings: Settings) -> None:
    """You may download the outputs of an RFP you own, or of a project you can open.

    A project's documents are published under its (32-character) id, so members who were never given the RFP
    itself can still download what the project produced. A project you can't open falls through to the RFP check,
    which answers 404 like any other unknown id.
    """
    if re.fullmatch(PROJECT_ID_PATTERN, rfp_id):
        store = store_for(settings)
        project = store.get(rfp_id, "project") if store is not None else None
        if project is not None and can_view(project, user, settings):
            return
    require_rfp_access(azure_clients.container(settings, settings.storage_rfp_container), rfp_id, user, settings)


@router.get("/outputs/{rfp_id}/{filename}")
def download_output(
    rfp_id: RfpId,
    filename: Filename,
    user: UserClaims = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    """Download a generated output file (draft.docx or compliance.xlsx) of an RFP you own or a project you can open."""
    if filename not in OUTPUT_FILENAMES:
        raise HTTPException(status_code=404, detail=f"Output not found: {rfp_id}/{filename}")
    _authorise(rfp_id, user, settings)

    blob_name = f"{rfp_id}/{filename}"
    try:
        data = azure_clients.container(settings, settings.storage_output_container).download_blob(blob_name).readall()
    except ResourceNotFoundError:
        raise HTTPException(status_code=404, detail=f"Output not found: {blob_name}") from None

    return StreamingResponse(
        io.BytesIO(data),
        media_type=_MEDIA_TYPES[filename[filename.rindex(".") :]],
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
