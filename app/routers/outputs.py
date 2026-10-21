"""Downloading generated documents."""

from __future__ import annotations

import io

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from app import azure_clients
from app.auth.access import require_rfp_access
from app.auth.entra import UserClaims, get_current_user
from app.config import Settings, get_settings
from app.validation import OUTPUT_FILENAMES, Filename, RfpId

router = APIRouter(tags=["outputs"])

_MEDIA_TYPES = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


@router.get("/outputs/{rfp_id}/{filename}")
def download_output(
    rfp_id: RfpId,
    filename: Filename,
    user: UserClaims = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    """Download a generated output file (draft.docx or compliance.xlsx) of an RFP you own."""
    if filename not in OUTPUT_FILENAMES:
        raise HTTPException(status_code=404, detail=f"Output not found: {rfp_id}/{filename}")
    require_rfp_access(azure_clients.container(settings, settings.storage_rfp_container), rfp_id, user, settings)

    blob_name = f"{rfp_id}/{filename}"
    try:
        data = azure_clients.container(settings, settings.storage_output_container).download_blob(blob_name).readall()
    except Exception:
        raise HTTPException(status_code=404, detail=f"Output not found: {blob_name}") from None

    return StreamingResponse(
        io.BytesIO(data),
        media_type=_MEDIA_TYPES[filename[filename.rindex(".") :]],
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
