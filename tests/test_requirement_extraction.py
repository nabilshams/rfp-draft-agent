"""Tests for RFP requirement extraction."""

import pytest

from app.ingestion.rfp_extract import (
    Requirement,
    RFPExtractionResult,
    extract_rfp_structure,
    extract_text_from_docx,
    extract_text_from_pdf,
)


# ── Sample RFP text ──────────────────────────────────────

SAMPLE_RFP_TEXT = """\
1. Introduction
This Request for Proposal (RFP) seeks qualified vendors to provide a cloud-based
document management system for Acme Corp.

2. Scope of Work
2.1 General Requirements
The vendor shall provide a web-based portal accessible 24/7.
The contractor must ensure 99.9% uptime availability.
The provider shall implement role-based access control (RBAC).

2.2 Technical Requirements
The vendor must support integration with Azure Active Directory.
The system shall provide RESTful APIs for all core operations.
The vendor should provide a mobile-responsive interface.

3. Evaluation Criteria
Proposals will be scored based on:
- Technical approach (40%)
- Experience and qualifications (30%)
- Cost proposal (30%)

4. Response Format
4.1 Submission Format
Responses must be submitted in PDF format.
Font size shall be no smaller than 11pt.
Page limit is 50 pages excluding appendices.

5. Questions
Does your platform support multi-tenant architecture?
What is your disaster recovery strategy?
Can you provide references from similar government contracts?
"""


class TestRequirementExtraction:
    def test_sections_are_extracted(self):
        result = extract_rfp_structure(SAMPLE_RFP_TEXT)
        assert len(result.sections) >= 4
        headings = [s["heading"] for s in result.sections]
        assert any("Introduction" in h for h in headings)
        assert any("Scope" in h or "2." in h for h in headings)

    def test_shall_must_requirements_found(self):
        result = extract_rfp_structure(SAMPLE_RFP_TEXT)
        assert len(result.requirements) >= 3
        req_texts = [r.text.lower() for r in result.requirements]
        assert any("uptime" in t or "99" in t for t in req_texts)
        assert any("rbac" in t or "role-based" in t or "access control" in t for t in req_texts)

    def test_requirements_have_mandatory_priority(self):
        result = extract_rfp_structure(SAMPLE_RFP_TEXT)
        priorities = {r.priority for r in result.requirements}
        assert "mandatory" in priorities

    def test_questions_extracted(self):
        result = extract_rfp_structure(SAMPLE_RFP_TEXT)
        assert len(result.questions) >= 2
        assert any("multi-tenant" in q.lower() for q in result.questions)
        assert any("disaster recovery" in q.lower() for q in result.questions)

    def test_evaluation_criteria_found(self):
        result = extract_rfp_structure(SAMPLE_RFP_TEXT)
        assert len(result.evaluation_criteria) >= 1

    def test_requirements_have_source_section(self):
        result = extract_rfp_structure(SAMPLE_RFP_TEXT)
        for req in result.requirements:
            assert req.source_section, f"Requirement '{req.text[:30]}' missing source_section"

    def test_requirement_ids_are_unique(self):
        result = extract_rfp_structure(SAMPLE_RFP_TEXT)
        ids = [r.id for r in result.requirements]
        assert len(ids) == len(set(ids))


class TestRequirementModel:
    def test_requirement_default_fields(self):
        req = Requirement(text="The vendor shall provide reporting.")
        assert req.id  # auto-generated
        assert req.category == "general"
        assert req.priority == "standard"
        assert req.keywords == []

    def test_requirement_serialization(self):
        req = Requirement(text="Must comply", category="security", priority="mandatory")
        data = req.model_dump()
        assert data["text"] == "Must comply"
        assert data["category"] == "security"


class TestEmptyInput:
    def test_empty_text_returns_empty_results(self):
        result = extract_rfp_structure("")
        assert isinstance(result, RFPExtractionResult)
        assert len(result.requirements) == 0
        assert len(result.questions) == 0
