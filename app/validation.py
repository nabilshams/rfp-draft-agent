"""Validation for values that end up in blob names, search filters and file names.

User-supplied ``client_id`` / ``project_id`` / ``solution_area`` / ``rfp_id`` / file names are used to
build blob paths and OData filters, so they must not contain path separators, dot segments or
control characters. The ``Annotated`` types below plug straight into request models and parameters;
FastAPI turns a failed check into a 422 response.
"""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import AfterValidator

# Letters, digits, dot, underscore, hyphen and inner spaces; must start with a letter or digit.
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,63}$")
_RFP_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_TONE_RE = re.compile(r"^[A-Za-z][A-Za-z -]{0,29}$")
_JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_MAX_FILENAME_LENGTH = 200

# The only files /draft produces; downloads are limited to these names.
OUTPUT_FILENAMES = frozenset({"draft.docx", "compliance.xlsx"})


def check_segment(value: str) -> str:
    """A client / project / solution-area identifier."""
    if not _SEGMENT_RE.fullmatch(value) or value != value.rstrip():
        raise ValueError(
            "must be 1-64 characters: letters, digits, '.', '_', '-' or spaces, starting with a letter or digit"
        )
    return value


def check_rfp_id(value: str) -> str:
    if not _RFP_ID_RE.fullmatch(value):
        raise ValueError("must be 1-64 letters, digits or hyphens")
    return value


def check_tone(value: str) -> str:
    """The writing tone goes into prompts, so it must be a short phrase, not free text."""
    if not _TONE_RE.fullmatch(value):
        raise ValueError("must be 1-30 letters, spaces or hyphens")
    return value


def check_job_id(value: str) -> str:
    if not _JOB_ID_RE.fullmatch(value):
        raise ValueError("must be a 32-character job id")
    return value


def check_filename(value: str) -> str:
    """A single file name: no path separators, dot segments or control characters."""
    if (
        not value
        or len(value) > _MAX_FILENAME_LENGTH
        or value in {".", ".."}
        or value != value.strip()
        or any(ch in value for ch in "/\\")
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)
    ):
        raise ValueError("must be a plain file name (no path separators or control characters)")
    return value


def check_kb_blob_name(value: str) -> str:
    """A knowledge-base blob name: ``client/project/solution_area/filename``."""
    parts = value.split("/")
    if len(parts) != 4:
        raise ValueError("must look like 'client/project/solution_area/filename'")
    for part in parts[:3]:
        check_segment(part)
    check_filename(parts[3])
    return value


Segment = Annotated[str, AfterValidator(check_segment)]
RfpId = Annotated[str, AfterValidator(check_rfp_id)]
Tone = Annotated[str, AfterValidator(check_tone)]
JobId = Annotated[str, AfterValidator(check_job_id)]
Filename = Annotated[str, AfterValidator(check_filename)]
KbBlobName = Annotated[str, AfterValidator(check_kb_blob_name)]
