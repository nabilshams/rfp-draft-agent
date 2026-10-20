"""RFP document parsing and structured requirement extraction.

Supports PDF (via PyPDF2) and DOCX (via python-docx).  Extracted text is
analysed for sections, "shall / must" requirements, questions, evaluation
criteria, and any response-format instructions.
"""

from __future__ import annotations

import logging
import re
import uuid
from io import BytesIO
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ── Pydantic models ──────────────────────────────────────

class Requirement(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    text: str
    category: str = "general"
    priority: str = "standard"
    source_section: str = ""
    keywords: list[str] = []


class RFPExtractionResult(BaseModel):
    sections: list[dict[str, Any]] = []
    requirements: list[Requirement] = []
    questions: list[str] = []
    evaluation_criteria: list[str] = []
    response_instructions: dict[str, Any] = {}


# ── Text extraction ──────────────────────────────────────

def extract_text_from_pdf(data: bytes) -> str:
    from PyPDF2 import PdfReader  # lazy import

    reader = PdfReader(BytesIO(data))
    pages: list[str] = []
    for page in reader.pages:
        text = page.extract_text()
        if text:
            pages.append(text)
    return "\n\n".join(pages)


def extract_text_from_docx(data: bytes) -> str:
    import docx  # lazy import

    doc = docx.Document(BytesIO(data))
    paragraphs: list[str] = []
    for para in doc.paragraphs:
        if para.text.strip():
            paragraphs.append(para.text.strip())
    return "\n".join(paragraphs)


def extract_text(filename: str, data: bytes) -> str:
    """Dispatch to the right parser based on file extension."""
    lower = filename.lower()
    if lower.endswith(".pdf"):
        return extract_text_from_pdf(data)
    elif lower.endswith(".docx"):
        return extract_text_from_docx(data)
    elif lower.endswith(".xlsx") or lower.endswith(".xls"):
        return _extract_text_from_excel(data)
    elif lower.endswith(".csv"):
        return _extract_text_from_csv(data)
    else:
        raise ValueError(f"Unsupported file type: {filename}")


def _extract_text_from_excel(data: bytes) -> str:
    """Extract text from an Excel file – each row becomes a line."""
    rows = _parse_excel_rows(data)
    lines: list[str] = []
    for row in rows:
        line = " | ".join(c for c in row if c)
        if line:
            lines.append(line)
    return "\n".join(lines)


def _extract_text_from_csv(data: bytes) -> str:
    """Extract text from a CSV file – each row becomes a line."""
    import csv
    import io as _io

    text = data.decode("utf-8-sig", errors="replace")
    reader = csv.reader(_io.StringIO(text))
    lines: list[str] = []
    for row in reader:
        cells = [c.strip() for c in row if c.strip()]
        if cells:
            lines.append(" | ".join(cells))
    return "\n".join(lines)


def extract_requirements_from_spreadsheet(filename: str, data: bytes) -> RFPExtractionResult:
    """Extract requirements directly from spreadsheet rows.

    Attempts to identify columns by header names (e.g. "Requirement",
    "Question", "Priority", "Category", "ID").  Falls back to treating
    the longest-text column as requirement text.
    """
    if filename.lower().endswith(".csv"):
        rows = _parse_csv_rows(data)
    else:
        rows = _parse_excel_rows(data)

    if not rows:
        logger.warning("Spreadsheet %s has no rows", filename)
        return RFPExtractionResult()

    logger.info("Spreadsheet %s: %d rows, %d columns", filename, len(rows), len(rows[0]) if rows else 0)
    logger.info("Header row: %s", rows[0] if rows else "empty")

    # Try to detect header row and map columns
    header = rows[0]
    col_map = _detect_columns(header)
    logger.info("Detected column mapping: %s", col_map)

    # If we detected at least a "text" column, skip header row
    has_header = bool(col_map.get("text") is not None)
    data_rows = rows[1:] if has_header else rows

    # If no text column was found, find the column with the longest average text
    if "text" not in col_map and data_rows:
        text_col = _find_longest_text_column(data_rows)
        col_map["text"] = text_col
        logger.info("Auto-detected text column index: %d", text_col)

    requirements: list[Requirement] = []
    questions: list[str] = []

    for i, row in enumerate(data_rows):
        if not any(cell.strip() for cell in row):
            continue

        text_idx = col_map.get("text", 0)
        text = _get_cell(row, text_idx)
        if not text or len(text.strip()) < 5:
            # Try other columns if the primary one is empty
            for ci in range(len(row)):
                if ci != text_idx and len(row[ci].strip()) >= 10:
                    text = row[ci].strip()
                    break
            if not text or len(text.strip()) < 5:
                continue

        text = text.strip()

        # Check if it's a question
        if text.endswith("?"):
            questions.append(text)
            continue

        req_id = _get_cell(row, col_map.get("id")) or str(uuid.uuid4())[:8]
        category = _get_cell(row, col_map.get("category")) or "general"
        priority = _get_cell(row, col_map.get("priority")) or _classify_priority(text)
        section = _get_cell(row, col_map.get("section")) or ""

        requirements.append(
            Requirement(
                id=req_id.strip(),
                text=text,
                category=category.strip().lower(),
                priority=priority.strip().lower(),
                source_section=section.strip(),
                keywords=_extract_keywords(text),
            )
        )

    logger.info("Extracted %d requirements, %d questions from %s", len(requirements), len(questions), filename)

    # If still nothing extracted, fall back to plain-text extraction
    if not requirements and not questions:
        logger.info("No structured requirements found, falling back to text extraction")
        raw_text = _extract_text_from_excel(data) if not filename.lower().endswith(".csv") else _extract_text_from_csv(data)
        return extract_rfp_structure(raw_text)

    return RFPExtractionResult(
        sections=[{"heading": "Spreadsheet Import", "body": f"{len(requirements)} requirements, {len(questions)} questions"}],
        requirements=requirements,
        questions=questions,
    )


def _parse_excel_rows(data: bytes) -> list[list[str]]:
    from io import BytesIO

    # Try openpyxl first (.xlsx), fall back to xlrd (.xls)
    try:
        from openpyxl import load_workbook
        wb = load_workbook(BytesIO(data), read_only=True, data_only=True)
        ws = wb.active
        rows: list[list[str]] = []
        for row in ws.iter_rows(values_only=True):
            rows.append([str(c).strip() if c is not None else "" for c in row])
        return rows
    except Exception:
        pass

    # Fall back to xlrd for .xls format
    try:
        import xlrd
        wb = xlrd.open_workbook(file_contents=data)
        ws = wb.sheet_by_index(0)
        rows = []
        for r in range(ws.nrows):
            rows.append([str(ws.cell_value(r, c)).strip() for c in range(ws.ncols)])
        return rows
    except Exception as exc:
        logger.error("Failed to parse Excel file: %s", exc)
        return []


def _parse_csv_rows(data: bytes) -> list[list[str]]:
    import csv
    import io as _io

    text = data.decode("utf-8-sig", errors="replace")
    reader = csv.reader(_io.StringIO(text))
    return [list(row) for row in reader]


_COLUMN_PATTERNS: dict[str, list[str]] = {
    "id": ["id", "req id", "requirement id", "ref", "reference", "#", "number", "no", "no.", "s.no", "sr", "sr.", "sl", "item"],
    "text": ["requirement", "description", "text", "question", "statement", "detail",
             "requirement text", "requirement description", "rfp question", "rfp requirement",
             "scope", "response required", "vendor response", "response", "ask",
             "deliverable", "criteria", "specification", "spec"],
    "category": ["category", "type", "area", "domain", "section type", "group", "topic", "module",
                 "requirement type"],
    "priority": ["priority", "importance", "criticality", "mandatory", "weight", "required",
                 "compliance", "level", "must have"],
    "section": ["section", "source", "source section", "module", "chapter", "page", "clause"],
}


def _find_longest_text_column(rows: list[list[str]], sample_size: int = 20) -> int:
    """Find the column index that contains the longest text on average."""
    if not rows:
        return 0
    sample = rows[:sample_size]
    num_cols = max(len(r) for r in sample)
    avg_lengths = []
    for col in range(num_cols):
        total = sum(len(r[col]) if col < len(r) else 0 for r in sample)
        avg_lengths.append(total / len(sample))
    if not avg_lengths:
        return 0
    return avg_lengths.index(max(avg_lengths))


def _detect_columns(header: list[str]) -> dict[str, int]:
    """Map logical column names to indices based on header text."""
    col_map: dict[str, int] = {}
    used_indices: set[int] = set()
    lower_header = [h.lower().strip() for h in header]

    # First pass: exact matches (higher priority)
    for logical_name, patterns in _COLUMN_PATTERNS.items():
        for i, h in enumerate(lower_header):
            if i in used_indices:
                continue
            if any(p == h for p in patterns):
                col_map[logical_name] = i
                used_indices.add(i)
                break

    # Second pass: substring matches for unmapped logical names
    for logical_name, patterns in _COLUMN_PATTERNS.items():
        if logical_name in col_map:
            continue
        # Sort patterns longest-first to prefer specific matches
        sorted_patterns = sorted(patterns, key=len, reverse=True)
        for i, h in enumerate(lower_header):
            if i in used_indices:
                continue
            if any(p in h for p in sorted_patterns):
                col_map[logical_name] = i
                used_indices.add(i)
                break

    # If no "text" column detected, use the first unmapped column with long values
    if "text" not in col_map:
        for i, h in enumerate(lower_header):
            if i not in used_indices and len(h) > 0:
                col_map["text"] = i
                break

    return col_map


def _get_cell(row: list[str], index: int | None) -> str:
    """Safely get a cell value by index."""
    if index is None or index >= len(row):
        return ""
    return row[index]


# ── Structured extraction ────────────────────────────────

_SECTION_RE = re.compile(
    r"^(?:\d+(?:\.\d+)*\.?\s+|[A-Z]\.\s+|Section\s+\d+)", re.MULTILINE
)

_SHALL_MUST_RE = re.compile(
    r"((?:the\s+)?(?:vendor|contractor|offeror|provider|respondent|proposer|bidder)?\s*"
    r"(?:shall|must|is required to|will be required to)\s+.+?)(?:\.|$)",
    re.IGNORECASE | re.MULTILINE,
)

_QUESTION_RE = re.compile(r"^.*\?\s*$", re.MULTILINE)

_EVAL_CRITERIA_RE = re.compile(
    r"(?:evaluation\s+criter|scoring\s+criter|selection\s+factor|weight(?:ing)?)",
    re.IGNORECASE,
)

_RESPONSE_INSTRUCTIONS_RE = re.compile(
    r"(?:response\s+format|submission\s+format|proposal\s+format|format\s+requirements"
    r"|page\s+limit|font\s+size|submission\s+instructions)",
    re.IGNORECASE,
)


def _extract_sections(text: str) -> list[dict[str, Any]]:
    """Split text into sections based on numbered headings."""
    matches = list(_SECTION_RE.finditer(text))
    if not matches:
        return [{"heading": "Full Document", "body": text}]

    sections: list[dict[str, Any]] = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        heading_line = text[start:end].split("\n", 1)[0].strip()
        body = text[start:end].strip()
        sections.append({"heading": heading_line, "body": body})
    return sections


def _classify_priority(text: str) -> str:
    lower = text.lower().strip()
    # Handle single-letter codes (M=mandatory, S=should/preferred)
    if lower in ("m", "mandatory", "critical", "required", "y", "yes"):
        return "mandatory"
    if lower in ("s", "should", "preferred", "desired", "d", "nice", "optional"):
        return "preferred"
    if lower in ("n", "no", "not required"):
        return "standard"
    # Fall back to keyword search in longer text
    if any(kw in lower for kw in ("mandatory", "critical", "must", "shall")):
        return "mandatory"
    if any(kw in lower for kw in ("should", "preferred", "desired")):
        return "preferred"
    return "standard"


def _extract_keywords(text: str) -> list[str]:
    """Naïve keyword extraction: capitalised phrases and technical terms."""
    words = re.findall(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b", text)
    return list(set(words))[:10]


def extract_rfp_structure(text: str) -> RFPExtractionResult:
    """Parse raw RFP text into structured components."""
    sections = _extract_sections(text)

    # Requirements (shall/must)
    requirements: list[Requirement] = []
    for sec in sections:
        for m in _SHALL_MUST_RE.finditer(sec["body"]):
            req_text = m.group(1).strip()
            if len(req_text) < 15:
                continue
            requirements.append(
                Requirement(
                    text=req_text,
                    category="compliance",
                    priority=_classify_priority(req_text),
                    source_section=sec["heading"],
                    keywords=_extract_keywords(req_text),
                )
            )

    # Questions
    questions = [m.group().strip() for m in _QUESTION_RE.finditer(text) if len(m.group().strip()) > 15]

    # Evaluation criteria
    eval_criteria: list[str] = []
    for sec in sections:
        if _EVAL_CRITERIA_RE.search(sec["heading"]):
            eval_criteria.append(sec["body"][:500])

    # Response format instructions
    response_instr: dict[str, Any] = {}
    for sec in sections:
        if _RESPONSE_INSTRUCTIONS_RE.search(sec["heading"]):
            response_instr["section"] = sec["heading"]
            response_instr["details"] = sec["body"][:500]

    return RFPExtractionResult(
        sections=[{"heading": s["heading"], "body": s["body"][:300]} for s in sections],
        requirements=requirements,
        questions=questions,
        evaluation_criteria=eval_criteria,
        response_instructions=response_instr,
    )
