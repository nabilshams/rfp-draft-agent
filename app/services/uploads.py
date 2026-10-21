"""Reading uploaded files."""

from __future__ import annotations

from fastapi import HTTPException, UploadFile

from app.config import Settings
from app.validation import check_filename


def validated_upload_filename(file: UploadFile, allowed_ext: tuple[str, ...]) -> str:
    if not file.filename:
        raise HTTPException(status_code=400, detail="Filename is required")
    try:
        check_filename(file.filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid file name: {exc}") from exc
    if not file.filename.lower().endswith(allowed_ext):
        raise HTTPException(status_code=400, detail=f"Only {allowed_ext} files are supported")
    return file.filename


def read_upload(file: UploadFile, settings: Settings) -> bytes:
    """Read an uploaded file, rejecting anything over MAX_UPLOAD_MB."""
    limit = settings.max_upload_mb * 1024 * 1024
    data = file.file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(status_code=413, detail=f"File too large (maximum {settings.max_upload_mb} MB)")
    return data
