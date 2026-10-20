"""Generate XLSX Compliance Matrix from agent draft output.

Columns:
  Requirement ID | Requirement Text | Priority | Meets (Yes/Partial/No) |
  Evidence/Notes | Source URL
"""

from __future__ import annotations

import io
import logging
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill, Border, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

logger = logging.getLogger(__name__)

_HEADER_FILL = PatternFill(start_color="1F4E79", end_color="1F4E79", fill_type="solid")
_HEADER_FONT = Font(color="FFFFFF", bold=True, size=11)
_THIN_BORDER = Border(
    left=Side(style="thin"),
    right=Side(style="thin"),
    top=Side(style="thin"),
    bottom=Side(style="thin"),
)

_COLUMNS = [
    ("Requirement ID", 18),
    ("Requirement Text", 55),
    ("Priority", 14),
    ("Meets (Yes/Partial/No)", 24),
    ("Evidence / Notes", 50),
    ("Source URL", 40),
]


def generate_xlsx(
    responses: list[dict[str, Any]],
    requirements: list[dict[str, Any]] | None = None,
) -> bytes:
    """Create a compliance-matrix workbook.

    *responses* come from the agent draft JSON.  *requirements* (optional)
    is the original extracted list and is used for the Requirement Text and
    Priority columns when available.
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "Compliance Matrix"

    # ── Header row ────────────────────────────────────────
    for col_idx, (header, width) in enumerate(_COLUMNS, start=1):
        cell = ws.cell(row=1, column=col_idx, value=header)
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = _THIN_BORDER
        ws.column_dimensions[get_column_letter(col_idx)].width = width

    # Build a lookup from requirement ID → original requirement data
    req_map: dict[str, dict] = {}
    if requirements:
        for req in requirements:
            rid = req.get("id", req.get("requirement_id", ""))
            req_map[rid] = req

    # ── Data rows ─────────────────────────────────────────
    for row_idx, resp in enumerate(responses, start=2):
        req_id = resp.get("requirement_id", "N/A")
        orig = req_map.get(req_id, {})

        req_text = orig.get("text", resp.get("answer_markdown", "")[:200])
        priority = orig.get("priority", "standard")
        meets = resp.get("meets", "N/A")

        # Consolidate evidence notes
        evidence_parts = []
        source_urls = []
        for ev in resp.get("evidence", []):
            if ev.get("quote"):
                evidence_parts.append(ev["quote"])
            if ev.get("title"):
                evidence_parts.append(f"[{ev['title']}]")
            if ev.get("source_url"):
                source_urls.append(ev["source_url"])

        evidence_text = "; ".join(evidence_parts) if evidence_parts else ""
        source_url_text = "\n".join(source_urls) if source_urls else ""

        row_data = [req_id, req_text, priority, meets, evidence_text, source_url_text]
        for col_idx, value in enumerate(row_data, start=1):
            cell = ws.cell(row=row_idx, column=col_idx, value=value)
            cell.alignment = Alignment(vertical="top", wrap_text=True)
            cell.border = _THIN_BORDER

    # ── Data validation for "Meets" column ────────────────
    meets_col = 4  # column D
    dv = DataValidation(type="list", formula1='"Yes,Partial,No"', allow_blank=True)
    dv.error = "Please select Yes, Partial, or No."
    dv.errorTitle = "Invalid value"
    ws.add_data_validation(dv)
    if len(responses) > 0:
        dv.add(f"D2:D{len(responses) + 1}")

    # Freeze header row
    ws.freeze_panes = "A2"

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.read()
