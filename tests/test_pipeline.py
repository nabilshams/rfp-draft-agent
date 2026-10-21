"""Redaction at every entry point that can reach a model."""

import httpx

from app.ingestion import pipeline
from app.main import (
    AnswerRequest,
    DraftRequest,
    GenerateDocRequest,
    GenerateSectionRequest,
    app,
)

SECRET = "Contact jane@example.com about the $50,000 budget"
CLEAN = "Contact [REDACTED-EMAIL] about the [REDACTED-PRICE] budget"


class TestRedactRecords:
    def test_free_text_in_nested_structures_is_redacted(self):
        data = [{"text": SECRET, "extra": {"notes": [SECRET, 3]}}]
        assert pipeline.redact_records(data) == [{"text": CLEAN, "extra": {"notes": [CLEAN, 3]}}]

    def test_identifiers_and_urls_are_left_alone(self):
        row = {
            "id": "REQ-1",
            "requirement_id": "REQ-1",
            "source_url": "https://kb/a@b.com/$100/doc.pdf",
            "meets": "Yes",
            "answer": SECRET,
        }
        out = pipeline.redact_records([row])[0]
        assert out["answer"] == CLEAN
        assert {k: out[k] for k in row if k != "answer"} == {k: row[k] for k in row if k != "answer"}

    def test_it_is_idempotent(self):
        once = pipeline.redact_records([{"text": SECRET}])
        assert pipeline.redact_records(once) == once


class TestRequestModels:
    def test_draft_request_redacts_context_and_caller_supplied_requirements(self):
        req = DraftRequest(
            rfp_id="r1",
            client_id="acme",
            project_id="p1",
            solution_area="cloud",
            context=SECRET,
            requirements=[{"id": "R1", "text": SECRET}],
        )
        assert req.context == CLEAN
        assert req.requirements[0] == {"id": "R1", "text": CLEAN}

    def test_answer_request_redacts_the_question(self):
        req = AnswerRequest(question=SECRET, client_id="acme", project_id="p1", solution_area="cloud")
        assert req.question == CLEAN

    def test_generate_section_request_redacts_prompt_and_responses(self):
        req = GenerateSectionRequest(
            rfp_id="r1",
            section_name="Overview",
            prompt=SECRET,
            responses=[{"requirement_id": "R1", "answer": SECRET}],
        )
        assert req.prompt == CLEAN
        assert req.responses[0] == {"requirement_id": "R1", "answer": CLEAN}

    def test_generate_doc_request_redacts_responses_and_narratives(self):
        req = GenerateDocRequest(
            rfp_id="r1",
            responses=[{"requirement_id": "R1", "answer": SECRET}],
            section_narratives=[{"section_name": "S", "narrative": SECRET}],
        )
        assert req.responses[0]["answer"] == CLEAN
        assert req.section_narratives[0]["narrative"] == CLEAN


class TestCustomerNames:
    def test_configured_customer_names_are_redacted_everywhere(self, monkeypatch):
        from app.config import Settings
        from app.ingestion import redaction

        settings = Settings(redaction_customer_names="Contoso Ltd;Fabrikam")
        monkeypatch.setattr(redaction, "get_settings", lambda: settings)
        monkeypatch.setattr(redaction, "_engine", None)
        assert pipeline.redact_text("We worked with contoso ltd and FABRIKAM.") == (
            "We worked with [REDACTED-CUSTOMER] and [REDACTED-CUSTOMER]."
        )


class TestExtraction:
    def test_spreadsheet_requirements_are_redacted(self):
        import io

        import openpyxl

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["ID", "Requirement"])
        ws.append(["R1", SECRET])
        buf = io.BytesIO()
        wb.save(buf)

        result = pipeline.extract_requirements("reqs.xlsx", buf.getvalue())

        assert result.requirements
        assert all("jane@example.com" not in r.text and "50,000" not in r.text for r in result.requirements)

    def test_csv_text_is_redacted(self):
        text = pipeline.redacted_document_text("reqs.csv", f"ID,Requirement\nR1,{SECRET}\n".encode())
        assert "jane@example.com" not in text
        assert "[REDACTED-EMAIL]" in text

    def test_document_text_is_redacted_before_structure_extraction(self):
        docx = open("tests/fixtures/sample_rfp.docx", "rb").read()
        result = pipeline.extract_requirements("rfp.docx", docx)
        assert result.requirements


async def test_answer_endpoint_passes_only_the_redacted_question(monkeypatch):
    from app.agents import foundry_agent

    seen = {}

    def fake(**kwargs):
        seen.update(kwargs)
        return {"answer": "ok"}

    monkeypatch.setattr(foundry_agent, "run_single_answer", fake)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        resp = await client.post(
            "/answer",
            json={"question": SECRET, "client_id": "acme", "project_id": "p1", "solution_area": "cloud"},
        )
    assert resp.status_code == 200
    assert seen["question"] == CLEAN
