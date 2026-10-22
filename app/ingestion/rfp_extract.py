"""RFP document parsing and structured requirement extraction.

Supports PDF (via PyPDF2), DOCX (via python-docx, including tables) and spreadsheets. Extracted text is
analysed for sections, "shall / must" requirements, questions, evaluation criteria, and any
response-format instructions.

Every requirement records where it came from (``anchor``: "p. 3", "¶12", "Table 2, row 5", "Row 14") and gets
a stable ID: the RFP's own reference when it has one, otherwise one derived from document order, so extracting the
same file twice gives the same IDs.
"""

from __future__ import annotations

import logging
import re
import uuid
from bisect import bisect_right
from collections.abc import Callable
from dataclasses import dataclass, field
from io import BytesIO
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

_SPREADSHEET_EXTENSIONS = (".xlsx", ".xls", ".csv")


# ── Pydantic models ──────────────────────────────────────

class Requirement(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    text: str
    # "question" for items phrased as a question; both kinds need an answer.
    type: str = "requirement"
    category: str = "general"
    priority: str = "standard"
    source_section: str = ""
    # Where in the RFP this came from: "p. 3", "¶12", "Table 2, row 5", "Row 14".
    anchor: str = ""
    keywords: list[str] = []


class RFPExtractionResult(BaseModel):
    sections: list[dict[str, Any]] = []
    requirements: list[Requirement] = []
    questions: list[str] = []
    evaluation_criteria: list[str] = []
    response_instructions: dict[str, Any] = {}


def requirement_type(text: str) -> str:
    return "question" if text.strip().endswith("?") else "requirement"


def assign_ids(requirements: list[Requirement]) -> None:
    """Give every requirement a unique, stable ID (in place).

    A reference the RFP supplies is kept. A missing one becomes ``R<n>`` from the requirement's position in the
    document, so extracting the same file again gives the same IDs. A reference that repeats gets a suffix
    (``3.1``, ``3.1-2``), because answers are matched back to requirements by ID.
    """
    used: set[str] = set()
    for position, requirement in enumerate(requirements, start=1):
        base = requirement.id.strip() or f"R{position}"
        candidate, copy = base, 1
        while candidate in used:
            copy += 1
            candidate = f"{base}-{copy}"
        used.add(candidate)
        requirement.id = candidate


def add_questions(requirements: list[Requirement], questions: list[str]) -> list[Requirement]:
    """Requirements plus any of *questions* they don't already contain (as requirements of type ``question``)."""
    known = [_normal(r.text) for r in requirements]
    result = list(requirements)
    for question in questions:
        key = _normal(question)
        if key and not any(key in k or k in key for k in known):
            result.append(Requirement(id="", text=question.strip(), type="question"))
            known.append(key)
    return result


def _normal(text: str) -> str:
    return " ".join(text.lower().split())


# ── Parsed documents ─────────────────────────────────────

@dataclass
class ParsedDocument:
    """A document read for extraction.

    ``text`` is everything readable (used for AI extraction and ingestion). ``structure_text`` is what the
    requirement patterns read: the same, minus tables that are parsed as rows of requirements instead.
    ``anchors`` are ``(offset into structure_text, label)`` in ascending order; ``table_rows`` are requirements
    taken from requirement tables, with the offset in ``structure_text`` where each table sits.
    """

    text: str = ""
    structure_text: str = ""
    anchors: list[tuple[int, str]] = field(default_factory=list)
    table_rows: list[tuple[int, Requirement]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._offsets = [offset for offset, _ in self.anchors]

    @classmethod
    def from_text(cls, text: str) -> ParsedDocument:
        return cls(text=text, structure_text=text)

    def anchor_at(self, offset: int) -> str:
        index = bisect_right(self._offsets, offset) - 1
        return self.anchors[index][1] if index >= 0 else ""


class _TextBuilder:
    """Joins lines with newlines while tracking where each one starts."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.length = 0

    def next_offset(self) -> int:
        return self.length + (1 if self.lines else 0)

    def add(self, line: str) -> int:
        start = self.next_offset()
        self.lines.append(line)
        self.length = start + len(line)
        return start

    def text(self) -> str:
        return "\n".join(self.lines)


# ── Text extraction ──────────────────────────────────────

def parse_pdf(data: bytes) -> ParsedDocument:
    """Read a PDF; each page is an anchor ("p. 3"). Pages are joined by a blank line."""
    from PyPDF2 import PdfReader  # lazy import

    reader = PdfReader(BytesIO(data))
    builder = _PageBuilder()
    for number, page in enumerate(reader.pages, start=1):
        text = page.extract_text()
        if text:
            builder.add(text, f"p. {number}")
    joined = builder.text()
    return ParsedDocument(text=joined, structure_text=joined, anchors=builder.anchors)


class _PageBuilder:
    def __init__(self) -> None:
        self.pages: list[str] = []
        self.anchors: list[tuple[int, str]] = []
        self._length = 0

    def add(self, text: str, label: str) -> None:
        start = self._length + (2 if self.pages else 0)
        self.anchors.append((start, label))
        self.pages.append(text)
        self._length = start + len(text)

    def text(self) -> str:
        return "\n\n".join(self.pages)


def parse_docx(data: bytes) -> ParsedDocument:
    """Read a Word document in body order: paragraphs *and* tables.

    A table whose header row names a requirement/question column (see ``_requirement_table_columns``) is read as
    one requirement per row, anchored "Table N, row M". Other tables are flattened into text lines so their
    content is still seen. Paragraph anchors are "¶N" (the Nth non-empty paragraph).
    """
    import docx  # lazy import
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    document = docx.Document(BytesIO(data))
    full = _TextBuilder()
    structure = _TextBuilder()
    anchors: list[tuple[int, str]] = []
    table_rows: list[tuple[int, Requirement]] = []
    paragraph_number = 0
    table_number = 0

    for child in document.element.body.iterchildren():
        kind = child.tag.rsplit("}", 1)[-1]
        if kind == "p":
            text = Paragraph(child, document).text.strip()
            if not text:
                continue
            paragraph_number += 1
            full.add(text)
            anchors.append((structure.add(text), f"¶{paragraph_number}"))
        elif kind == "tbl":
            table_number += 1
            rows = _table_rows(Table(child, document))
            if not any(any(row) for row in rows):
                continue
            columns = _requirement_table_columns(rows[0])
            for row_number, row in enumerate(rows, start=1):
                line = " | ".join(cell for cell in row if cell)
                if line:
                    full.add(line)
                    if columns is None:
                        anchors.append((structure.add(line), f"Table {table_number}, row {row_number}"))
            if columns is not None:
                # Just before the next line of text, so the table belongs to the section it sits in.
                offset = max(structure.next_offset() - 1, 0)
                found = _rows_to_requirements(
                    rows[1:], columns, lambda i, n=table_number: f"Table {n}, row {i + 2}"
                )
                table_rows.extend((offset, requirement) for requirement in found)

    return ParsedDocument(
        text=full.text(), structure_text=structure.text(), anchors=anchors, table_rows=table_rows
    )


def _table_rows(table: Any) -> list[list[str]]:
    rows: list[list[str]] = []
    for row in table.rows:
        seen: set[int] = set()
        cells: list[str] = []
        for cell in row.cells:
            if id(cell._tc) in seen:  # a merged cell is reported once per column it spans
                continue
            seen.add(id(cell._tc))
            cells.append(" ".join(cell.text.split()))
        rows.append(cells)  # empty rows are kept so row numbers match the document
    return rows





def parse_document(filename: str, data: bytes) -> ParsedDocument:
    """Read a PDF or Word RFP for structure extraction (spreadsheets have their own path)."""
    lower = filename.lower()
    if lower.endswith(".pdf"):
        return parse_pdf(data)
    if lower.endswith(".docx"):
        return parse_docx(data)
    raise ValueError(f"Unsupported file type: {filename}")


def extract_text(filename: str, data: bytes) -> str:
    """Dispatch to the right parser based on file extension."""
    lower = filename.lower()
    if lower.endswith((".pdf", ".docx")):
        return parse_document(filename, data).text
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


def guidance_text(extraction: RFPExtractionResult) -> str:
    """The RFP's evaluation criteria and response instructions as prompt text (empty if it has neither)."""
    parts: list[str] = []
    if extraction.evaluation_criteria:
        parts.append("Evaluation criteria:\n" + "\n\n".join(extraction.evaluation_criteria))
    details = extraction.response_instructions.get("details")
    if details:
        parts.append("Response instructions:\n" + str(details))
    return "\n\n".join(parts)


def extract_from_file(filename: str, data: bytes) -> RFPExtractionResult:
    """Structured extraction for any supported RFP file (PDF, Word, spreadsheet), unredacted."""
    if filename.lower().endswith(_SPREADSHEET_EXTENSIONS):
        return extract_requirements_from_spreadsheet(filename, data)
    document = parse_document(filename, data)
    return extract_rfp_structure(document.text, document)


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

    # A first row that names no text column but holds a long sentence is data, not a header
    has_header = _looks_like_header(header)
    if not has_header:
        col_map = {}
    data_rows = rows[1:] if has_header else rows

    # If no text column was found, find the column with the longest average text
    if "text" not in col_map and data_rows:
        text_col = _find_longest_text_column(data_rows)
        col_map["text"] = text_col
        logger.info("Auto-detected text column index: %d", text_col)

    requirements = _rows_to_requirements(
        data_rows, col_map, lambda i: f"Row {i + (2 if has_header else 1)}"
    )
    assign_ids(requirements)
    questions = [r.text for r in requirements if r.type == "question"]

    logger.info("Extracted %d requirements (%d questions) from %s", len(requirements), len(questions), filename)

    # If still nothing extracted, fall back to plain-text extraction
    if not requirements:
        logger.info("No structured requirements found, falling back to text extraction")
        raw_text = _extract_text_from_excel(data) if not filename.lower().endswith(".csv") else _extract_text_from_csv(data)
        return extract_rfp_structure(raw_text)

    return RFPExtractionResult(
        sections=[{"heading": "Spreadsheet Import", "body": f"{len(requirements)} requirements, {len(questions)} questions"}],
        requirements=requirements,
        questions=questions,
    )


def _rows_to_requirements(
    rows: list[list[str]], col_map: dict[str, int], anchor: Callable[[int], str]
) -> list[Requirement]:
    """One requirement per row; *anchor* maps the row's index in *rows* to a label such as "Row 14".

    A row ending in "?" is a question: it is kept (with its ID, priority and category) as a requirement of type
    ``question`` so that it gets an answer like any other. IDs are left blank unless the sheet supplies one;
    :func:`assign_ids` fills them in.
    """
    requirements: list[Requirement] = []
    for i, row in enumerate(rows):
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
        category = _get_cell(row, col_map.get("category")) or "general"
        priority = _get_cell(row, col_map.get("priority")) or _classify_priority(text)

        requirements.append(
            Requirement(
                id=_get_cell(row, col_map.get("id")).strip(),
                text=text,
                type=requirement_type(text),
                category=category.strip().lower(),
                priority=priority.strip().lower(),
                source_section=_get_cell(row, col_map.get("section")).strip(),
                anchor=anchor(i),
                keywords=_extract_keywords(text),
            )
        )
    return requirements


def _looks_like_header(row: list[str]) -> bool:
    """A header names a text column, or at least has only short labels (no sentence-length cell)."""
    if "text" in _detect_columns(row, guess_text=False):
        return True
    return max((len(cell.split()) for cell in row), default=0) < _HEADER_CELL_MAX_WORDS


_HEADER_CELL_MAX_WORDS = 6


# A table is read as requirements only when a header names the text column this way; a "Description" column
# in a pricing or staffing table must not turn its rows into requirements.
_TABLE_TEXT_HEADERS = ("requirement", "question", "specification", "statement")


def _requirement_table_columns(header: list[str]) -> dict[str, int] | None:
    """Column mapping for a table of requirements/questions, or None if the table is something else."""
    columns = _detect_columns(header, guess_text=False)
    text = columns.get("text")
    if text is None or not any(word in header[text].lower() for word in _TABLE_TEXT_HEADERS):
        return None
    return columns


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


def _detect_columns(header: list[str], guess_text: bool = True) -> dict[str, int]:
    """Map logical column names to indices based on header text.

    With *guess_text*, a header that names no text column falls back to its first unmapped column.
    """
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
    if guess_text and "text" not in col_map:
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


_GUIDANCE_CHARS = 1500


def _section_spans(text: str) -> list[tuple[int, str, str]]:
    """Split text into ``(start offset, heading, body)`` sections based on numbered headings."""
    matches = list(_SECTION_RE.finditer(text))
    if not matches:
        return [(0, "Full Document", text)]

    spans: list[tuple[int, str, str]] = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        heading_line = text[start:end].split("\n", 1)[0].strip()
        spans.append((start, heading_line, text[start:end].strip()))
    return spans


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


def extract_rfp_structure(text: str, document: ParsedDocument | None = None) -> RFPExtractionResult:
    """Parse RFP text into structured components.

    With a parsed *document*, requirements also carry where they came from (page, paragraph or table row) and
    requirement tables are included; plain *text* only gives section names.
    """
    document = document or ParsedDocument.from_text(text)
    body = document.structure_text
    spans = _section_spans(body)
    starts = [start for start, _, _ in spans]

    def section_at(offset: int) -> str:
        index = bisect_right(starts, offset) - 1
        return spans[index][1] if index >= 0 else ""

    # (offset in the text, requirement), merged and ordered by position in the document
    found: list[tuple[int, Requirement]] = []

    # Requirements (shall/must)
    statement_spans: list[tuple[int, int]] = []
    for m in _SHALL_MUST_RE.finditer(body):
        req_text = m.group(1).strip()
        if len(req_text) < 15:
            continue
        offset = m.start(1) + (len(m.group(1)) - len(m.group(1).lstrip()))
        statement_spans.append((m.start(), m.end()))
        found.append(
            (
                offset,
                Requirement(
                    id="",
                    text=req_text,
                    type=requirement_type(req_text),
                    category="compliance",
                    priority=_classify_priority(req_text),
                    source_section=section_at(offset),
                    anchor=document.anchor_at(offset),
                    keywords=_extract_keywords(req_text),
                ),
            )
        )

    # Questions (a line that is already a "shall/must" statement is not counted twice)
    for m in _QUESTION_RE.finditer(body):
        question = m.group().strip()
        if len(question) <= 15 or any(s < m.end() and m.start() < e for s, e in statement_spans):
            continue
        offset = m.start() + (len(m.group()) - len(m.group().lstrip()))
        found.append(
            (
                offset,
                Requirement(
                    id="",
                    text=question,
                    type="question",
                    source_section=section_at(offset),
                    anchor=document.anchor_at(offset),
                    keywords=_extract_keywords(question),
                ),
            )
        )

    for offset, requirement in document.table_rows:
        requirement.source_section = requirement.source_section or section_at(offset)
        found.append((offset, requirement))

    found.sort(key=lambda item: item[0])
    requirements = [requirement for _, requirement in found]
    assign_ids(requirements)

    # Evaluation criteria
    eval_criteria = [
        text_[:_GUIDANCE_CHARS] for _, heading, text_ in spans if _EVAL_CRITERIA_RE.search(heading)
    ]

    # Response format instructions (every matching section, not just the last)
    instruction_sections = [(h, t) for _, h, t in spans if _RESPONSE_INSTRUCTIONS_RE.search(h)]
    response_instr: dict[str, Any] = {}
    if instruction_sections:
        response_instr["section"] = "; ".join(h for h, _ in instruction_sections)
        response_instr["details"] = "\n\n".join(t[:_GUIDANCE_CHARS] for _, t in instruction_sections)

    return RFPExtractionResult(
        sections=[{"heading": heading, "body": text_[:300]} for _, heading, text_ in spans],
        requirements=requirements,
        questions=[r.text for r in requirements if r.type == "question"],
        evaluation_criteria=eval_criteria,
        response_instructions=response_instr,
    )
