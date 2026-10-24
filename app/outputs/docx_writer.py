"""Generate DOCX RFP Response drafts from a template.

Uses ``python-docx`` to fill placeholders in the template file
``/templates/rfp_response_template.docx``.  If the template is missing,
a new document is created from scratch.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import Any

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH

from app.agents import confidence, states, verify
from app.ingestion.provenance import describe_source

logger = logging.getLogger(__name__)

_TEMPLATE_PATH = Path(__file__).resolve().parents[2] / "templates" / "rfp_response_template.docx"


def parse_template_sections(template_path: Path | None = None) -> list[dict[str, Any]]:
    """Parse a DOCX template and extract its section structure.

    Returns a list of dicts:
      { "heading": str, "level": int, "placeholder": str|None, "body_preview": str }
    """
    path = template_path or _TEMPLATE_PATH
    if not path.exists():
        return []

    try:
        doc = Document(str(path))
    except Exception:
        return []

    sections: list[dict[str, Any]] = []
    current_heading: str | None = None
    current_level: int = 0
    current_body: list[str] = []

    for para in doc.paragraphs:
        style_name = (para.style.name or "").lower()

        # Detect headings
        if "heading" in style_name:
            # Save previous section
            if current_heading is not None:
                body_text = "\n".join(current_body).strip()
                placeholder = None
                if "{{" in body_text:
                    import re
                    m = re.search(r"\{\{(\w+)\}\}", body_text)
                    if m:
                        placeholder = m.group(1)
                sections.append({
                    "heading": current_heading,
                    "level": current_level,
                    "placeholder": placeholder,
                    "body_preview": body_text[:200],
                })
            # Start new section
            current_heading = para.text.strip()
            try:
                current_level = int(style_name.replace("heading", "").strip())
            except ValueError:
                current_level = 1
            current_body = []
        elif current_heading is not None:
            if para.text.strip():
                current_body.append(para.text.strip())

    # Save last section
    if current_heading is not None:
        body_text = "\n".join(current_body).strip()
        placeholder = None
        if "{{" in body_text:
            import re
            m = re.search(r"\{\{(\w+)\}\}", body_text)
            if m:
                placeholder = m.group(1)
        sections.append({
            "heading": current_heading,
            "level": current_level,
            "placeholder": placeholder,
            "body_preview": body_text[:200],
        })

    return sections


def _default_section_content(heading: str, draft: dict[str, Any]) -> str:
    """Fallback content for a template heading that has no placeholder or section narrative.

    Real templates use instructional text instead of ``{{placeholders}}``, so the draft's
    executive summary and solution overview are matched by heading text.
    """
    lowered = heading.lower()
    if "executive summary" in lowered:
        return draft.get("executive_summary", "")
    if "solution overview" in lowered:
        return draft.get("solution_overview", "")
    return ""


def generate_docx(draft: dict[str, Any]) -> bytes:
    """Create a DOCX from the agent draft JSON.

    Returns raw bytes of the DOCX file.
    """
    # Always generate a clean output document. We use template sections only as
    # structure metadata, not as literal source content, to avoid shipping
    # instructional template text in the final output.
    doc = Document()

    template_sections = parse_template_sections()

    customer_name = (
        draft.get("customer_name")
        or draft.get("client_name")
        or draft.get("client_id")
        or "Customer Name"
    )

    # -- Required front matter -----------------------------
    title_para = doc.add_heading(f"{customer_name} RFP RESPONSE", level=0)
    title_para.alignment = WD_ALIGN_PARAGRAPH.CENTER

    doc.add_heading("Cover Page", level=1)
    doc.add_paragraph(f"Response Prepared For: {customer_name}")
    doc.add_paragraph("Prepared By: [Your Organization]")
    doc.add_paragraph("Date: [Insert Date]")
    doc.add_page_break()

    doc.add_heading("Confidentiality Notice", level=1)
    doc.add_paragraph(
        "This proposal contains confidential and proprietary information intended "
        "solely for the recipient organization for RFP evaluation purposes. "
        "No part of this document may be reproduced, distributed, or disclosed "
        "without prior written permission."
    )
    doc.add_page_break()

    responses = draft.get("responses", [])
    section_narratives = draft.get("section_narratives", [])
    narrative_by_heading = {
        str(s.get("section_name", "")).strip().lower(): s.get("narrative_markdown", "")
        for s in section_narratives
    }

    compliance_summary = ""
    if responses:
        summary_lines = [f"- {r.get('requirement_id', 'N/A')}: {r.get('meets') or 'Not assessed'}" for r in responses]
        compliance_summary = "\n".join(summary_lines)

    # -- Main narrative sections ---------------------------
    if template_sections:
        logger.info("Using %d template sections as output structure", len(template_sections))
        for sec in template_sections:
            heading = str(sec.get("heading", "")).strip()
            if not heading:
                continue

            level = int(sec.get("level", 1))
            if level < 1:
                level = 1
            if level > 9:
                level = 9

            doc.add_heading(heading, level=level)

            placeholder = (sec.get("placeholder") or "").strip().lower()
            content = ""
            if placeholder == "executive_summary":
                content = draft.get("executive_summary", "")
            elif placeholder == "solution_overview":
                content = draft.get("solution_overview", "")
            elif placeholder == "compliance_matrix_summary":
                content = compliance_summary
            else:
                content = narrative_by_heading.get(heading.lower(), "") or _default_section_content(heading, draft)

            doc.add_paragraph(content or "Not provided.")
    else:
        logger.info("No template structure found; using default narrative sections")
        doc.add_heading("Executive Summary", level=1)
        doc.add_paragraph(draft.get("executive_summary", "Not provided."))

        doc.add_heading("Solution Overview", level=1)
        doc.add_paragraph(draft.get("solution_overview", "Not provided."))

        if section_narratives:
            for section in section_narratives:
                section_name = section.get("section_name", "Untitled Section")
                narrative = section.get("narrative_markdown", "")
                doc.add_heading(str(section_name), level=2)
                doc.add_paragraph(narrative or "Not provided.")

    # Detailed Responses
    doc.add_heading("Detailed Responses", level=1)
    for resp in responses:
        req_id = resp.get("requirement_id", "N/A")
        doc.add_heading(f"Requirement {req_id}", level=2)

        # Compliance status
        meets = resp.get("meets") or "Not assessed"
        status_para = doc.add_paragraph()
        status_run = status_para.add_run(f"Compliance: {meets}")
        status_run.bold = True
        state = states.LABELS.get(str(resp.get("state", "")))
        if state:
            state_run = status_para.add_run(f"   Status: {state}")
            state_run.bold = True
        if resp.get("missing"):
            doc.add_paragraph(f"Missing: {resp['missing']}")
        verification = verify.describe(resp.get("verification"))
        if verification:
            doc.add_paragraph(f"Verification: {verification}")
        band = confidence.describe(resp.get("confidence"), reasons=5)
        if band:
            doc.add_paragraph(f"Confidence: {band}")

        # Answer
        doc.add_paragraph(resp.get("answer_markdown", ""))

        # Evidence / citations
        evidence = resp.get("evidence", [])
        if evidence:
            doc.add_heading("Sources", level=3)
            for ev in evidence:
                title = ev.get("title", "Unknown")
                url = ev.get("source_url", "")
                quote = ev.get("quote", "")
                doc.add_paragraph(f"• {title}: {url}")
                label = describe_source(ev)
                if label:
                    doc.add_paragraph(f"  ({label})")
                if quote:
                    quote_para = doc.add_paragraph(f'  "{quote}"')
                    quote_para.style = doc.styles["Quote"] if "Quote" in [s.name for s in doc.styles] else None

        # Assumptions & Risks
        for label, key in [("Assumptions", "assumptions"), ("Risks", "risks"), ("Follow-ups", "followups")]:
            items = resp.get(key, [])
            if items:
                doc.add_heading(label, level=3)
                for item in items:
                    doc.add_paragraph(f"• {item}")

    # Serialize to bytes
    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return buf.read()
