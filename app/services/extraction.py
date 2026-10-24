"""Reading an RFP with the model, as the API uses it: the extraction, with failures turned into HTTP answers."""

from __future__ import annotations

from fastapi import HTTPException

from app.config import Settings
from app.ingestion import ai_extract
from app.ingestion.pipeline import redact_extraction
from app.ingestion.rfp_extract import RFPExtractionResult


def extract_with_model(filename: str, data: bytes, settings: Settings) -> RFPExtractionResult:
    """Every part of the RFP read by the model (see :mod:`app.ingestion.ai_extract`), with every field redacted.

    A document too long to read in full is a 413 and a part the model could not read is a 502: nothing is cut or skipped.
    """
    try:
        return redact_extraction(ai_extract.extract(filename, data, settings))
    except ai_extract.ExtractionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None
