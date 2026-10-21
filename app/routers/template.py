"""The shared DOCX response template."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, UploadFile

from app.auth.access import require_admin
from app.auth.entra import UserClaims, get_current_user
from app.auth.ratelimit import rate_limited
from app.config import Settings, get_settings
from app.outputs.docx_writer import parse_template_sections
from app.services.uploads import read_upload, validated_upload_filename

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/template", tags=["template"])

TEMPLATE_PATH = Path(__file__).resolve().parent.parent.parent / "templates" / "rfp_response_template.docx"

_OLE2_SIGNATURE = bytes.fromhex("D0CF11E0A1B11AE1")


def _format_warning(data: bytes) -> str | None:
    """A message when the file isn't a .docx (a .docx is a ZIP archive)."""
    if data[:2] == b"PK":
        return None
    if data[:8] == _OLE2_SIGNATURE:
        return (
            "This file is in the old .doc (OLE2) format, not .docx. "
            "Please re-save it in Word as 'Word Document (.docx)' format "
            "(File → Save As → choose 'Word Document (.docx)'). "
            "The template has been saved but section detection will not work "
            "until it is in .docx format."
        )
    return "This file does not appear to be a valid Word document."


@router.post("/upload", dependencies=[Depends(rate_limited)])
def template_upload(
    file: UploadFile = File(...),
    user: UserClaims = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    """Upload the DOCX template used for every RFP response (administrators only: it is shared)."""
    require_admin(user, settings)
    filename = validated_upload_filename(file, (".docx", ".doc"))

    data = read_upload(file, settings)
    warning = _format_warning(data)

    TEMPLATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    TEMPLATE_PATH.write_bytes(data)
    logger.info("Template uploaded: %s (%d bytes) → %s", filename, len(data), TEMPLATE_PATH)

    result = {
        "filename": filename,
        "size_bytes": len(data),
        "sections": parse_template_sections(TEMPLATE_PATH),
        "message": "Template saved. It will be used for all future draft generations.",
    }
    if warning:
        result["warning"] = warning
    return result


@router.get("/info")
def template_info():
    """Check whether a custom template is loaded and return its sections."""
    if TEMPLATE_PATH.exists():
        stat = TEMPLATE_PATH.stat()
        return {
            "exists": True,
            "size_bytes": stat.st_size,
            "last_modified": datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
            "sections": parse_template_sections(TEMPLATE_PATH),
        }
    return {"exists": False, "sections": []}
