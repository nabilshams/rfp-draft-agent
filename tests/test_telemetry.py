"""Telemetry: one trace per draft with a span per stage, tokens and cost, and never any content."""

import json
import logging
import threading
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app import azure_clients
from app.agents import kb_search, lifecycle, runner, sections
from app.auth.entra import UserClaims
from app.outputs import docx_writer
from app.schemas import DraftRequest
from app.services import draft_pipeline, jobs
from app.telemetry import spans, tracing

from .test_authorization_api import make_settings
from .test_draft_jobs import BODY, drafts, start, wait_for, worker  # noqa: F401  (fixtures and helpers)
from .test_kb_search import FakeSearchClient, hit

SECRET_REQUIREMENT = "The vendor shall keep ZEBRA-CONFIDENTIAL-7731 safe."
SECRET_ANSWER = "Our private design for ZEBRA-CONFIDENTIAL-7731 is described in the attached internal memo."
USER = UserClaims(oid="user-a", groups=[])


@pytest.fixture
def exporter(monkeypatch):
    memory = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(memory))
    monkeypatch.setattr(spans, "get_tracer", lambda name=spans.TRACER_NAME: provider.get_tracer(name))
    return memory


@pytest.fixture
def model(monkeypatch):
    """The model: 1000 input and 200 output tokens per call; replies with SECRET_ANSWER in an answer object."""
    state = SimpleNamespace(calls=0, status="completed")

    def run(agent_id, content, *, web_search=False):
        state.calls += 1
        reply = json.dumps({"requirement_id": "R1", "answer_markdown": SECRET_ANSWER, "meets": "Yes", "evidence": []})
        return runner.AgentRunResult(
            status=state.status, text=reply, has_response=True, usage={"input_tokens": 1000, "output_tokens": 200}
        )

    monkeypatch.setattr(runner, "_run_agent_prompt", run)
    monkeypatch.setattr(lifecycle, "ensure_agent", lambda name: "agent-1")
    monkeypatch.setattr(kb_search, "embed_queries", lambda queries: [None] * len(queries))
    monkeypatch.setattr(
        azure_clients, "search_client", lambda _s: FakeSearchClient([hit(content="Data is encrypted with AES-256.")])
    )
    monkeypatch.setattr(runner, "get_settings", lambda: make_settings())
    return state


def run_draft(world, approach="per_requirement", requirements=None, job_id="job-123", **kwargs):
    req = DraftRequest(rfp_id="rfp-1", client_id="acme", project_id="p1", approach=approach)
    requirements = requirements or [
        {"id": "R1", "text": SECRET_REQUIREMENT},
        {"id": "R2", "text": "Support is available around the clock."},
    ]
    events = list(
        draft_pipeline.stream_draft(make_settings(**kwargs), req, requirements, USER, False, "", job_id)
    )
    return events


def by_name(exporter):
    found = {}
    for span in exporter.get_finished_spans():
        found.setdefault(span.name, []).append(span)
    return found


# ── The shape of a draft's trace ─────────────────────────


class TestTrace:
    def test_one_trace_per_draft_with_a_span_for_each_requirement_and_its_stages(self, world, exporter, model):
        run_draft(world)
        spans_by_name = by_name(exporter)

        (draft,) = spans_by_name["draft"]
        assert len({s.context.trace_id for s in exporter.get_finished_spans()}) == 1
        requirements = spans_by_name["requirement"]
        assert [s.attributes["requirement.id"] for s in requirements] == ["R1", "R2"]
        assert all(s.parent.span_id == draft.context.span_id for s in requirements)

        for requirement in requirements:
            children = {s.name: s for s in exporter.get_finished_spans() if s.parent and s.parent.span_id == requirement.context.span_id}
            assert set(children) == {"kb.search", "llm.run"}
            assert children["llm.run"].attributes["llm.model"] == "gpt-4o"

    def test_the_model_span_has_status_latency_tokens_and_cost(self, world, exporter, model):
        run_draft(world)
        llm = by_name(exporter)["llm.run"][0].attributes
        assert llm["llm.status"] == "completed" and llm["outcome"] == "ok"
        assert llm["llm.input_tokens"] == 1000 and llm["llm.output_tokens"] == 200 and llm["llm.total_tokens"] == 1200
        assert llm["llm.cost_usd"] == pytest.approx(0.0045)
        assert llm["llm.latency_ms"] >= 0 and llm["prompt.chars"] > 0

    def test_the_retrieval_span_says_how_many_documents_and_how(self, world, exporter, model):
        run_draft(world)
        search = by_name(exporter)["kb.search"][0].attributes
        assert search["kb.results"] == 1 and search["kb.mode"] == "keyword"

    def test_the_draft_span_carries_the_job_and_the_totals(self, world, exporter, model):
        run_draft(world)
        draft = by_name(exporter)["draft"][0].attributes
        assert draft["job.id"] == "job-123" and draft["rfp.id"] == "rfp-1" and draft["approach"] == "per_requirement"
        assert (draft["requirements.total"], draft["requirements.done"]) == (2, 2)
        assert (draft["llm.calls"], draft["llm.total_tokens"]) == (2, 2400)
        assert draft["llm.cost_usd"] == pytest.approx(0.009)  # visible in Application Insights per draft
        assert draft["outcome"] == "ok"

    def test_storing_the_documents_is_a_stage_with_its_size(self, world, exporter, model):
        run_draft(world)
        (outputs,) = by_name(exporter)["outputs"]
        assert outputs.attributes["outputs.bytes"] > 0

    def test_prices_come_from_settings(self, world, exporter, model):
        run_draft(world, model_input_price_per_million=0.0, model_output_price_per_million=0.0)
        assert by_name(exporter)["draft"][0].attributes["llm.cost_usd"] == 0.0

    def test_a_section_draft_adds_classification_and_narrative_stages_with_their_own_model_calls(self, world, exporter, model):
        run_draft(world, approach="map_reduce")
        names = by_name(exporter)
        assert len(names["section.classify"]) == 1 and names["section.narrative"]
        draft = names["draft"][0]
        # every model call belongs to the draft's trace
        assert all(s.context.trace_id == draft.context.trace_id for s in names["llm.run"])
        assert draft.attributes["llm.calls"] == len(names["llm.run"]) > 2

    def test_sections_written_in_parallel_stay_in_the_trace_and_the_meter(self, exporter, model):
        meter = spans.Meter(input_price=2.5, output_price=10.0)
        draft = spans.DraftTrace("draft", meter)

        def generate(**job):
            runner.run_agent_prompt("a", "p")
            return {"section_name": job["section_name"]}

        with draft.step():
            written = list(
                sections.write_sections(
                    [{"section_name": "A", "req_ids": []}, {"section_name": "B", "req_ids": []}], generate, workers=2
                )
            )
        draft.finish("ok")

        assert len(written) == 2 and meter.calls == 2
        narratives = by_name(exporter)["section.narrative"]
        assert {s.parent.span_id for s in narratives} == {by_name(exporter)["draft"][0].context.span_id}

class TestJobs:
    async def test_a_draft_job_records_its_id_on_the_draft_span(self, client, world, exporter, worker, drafts):  # noqa: F811
        started = (await start(client)).json()
        worker.run_all()
        await wait_for(client, started["job_id"], lambda job: job["status"] == "succeeded")

        (draft,) = by_name(exporter)["draft"]
        assert draft.attributes["job.id"] == started["job_id"]
        assert draft.attributes["outcome"] == "ok"
        assert jobs  # the job module is what passes the id through


# ── Generators resumed on other threads, cancellation and failure ──


class TestLifecycle:
    def test_a_draft_resumed_on_different_threads_keeps_one_trace_and_detaches_cleanly(self, world, exporter, model, caplog):
        caplog.set_level(logging.ERROR)
        req = DraftRequest(rfp_id="rfp-1", client_id="acme", project_id="p1")
        requirements = [{"id": "R1", "text": "one"}, {"id": "R2", "text": "two"}]
        events = draft_pipeline.stream_draft(make_settings(), req, requirements, USER, False, "", "job-t")
        seen = []

        def step():
            try:
                seen.append(next(events))
            except StopIteration:
                seen.append(None)

        while not seen or seen[-1] is not None:
            thread = threading.Thread(target=step)  # the way a streaming response resumes a generator
            thread.start()
            thread.join()

        assert [e["type"] for e in seen if e] == ["start", "response", "response", "complete"]
        assert len({s.context.trace_id for s in exporter.get_finished_spans()}) == 1
        assert by_name(exporter)["draft"][0].attributes["llm.calls"] == 2
        assert not [r for r in caplog.records if "detach" in r.getMessage().lower()]

    def test_a_draft_closed_early_ends_as_cancelled_with_what_it_spent(self, world, exporter, model):
        req = DraftRequest(rfp_id="rfp-1", client_id="acme", project_id="p1")
        events = draft_pipeline.stream_draft(make_settings(), req, [{"id": "R1", "text": "one"}, {"id": "R2", "text": "two"}], USER, False)
        next(events)  # start
        next(events)  # first response
        events.close()

        draft = by_name(exporter)["draft"][0].attributes
        assert draft["outcome"] == "cancelled" and draft["requirements.done"] == 1 and draft["llm.calls"] == 1

    def test_a_failure_ends_the_draft_as_an_error_with_only_the_exception_type(self, world, exporter, model, monkeypatch):
        def broken(*_a, **_kw):
            raise HTTPException(status_code=502, detail=f"upload failed for {SECRET_REQUIREMENT}")

        monkeypatch.setattr(draft_pipeline, "publish_outputs", broken)
        with pytest.raises(HTTPException):
            run_draft(world)

        draft = by_name(exporter)["draft"][0]
        assert draft.attributes["outcome"] == "error" and draft.attributes["error.type"] == "HTTPException"
        assert draft.status.status_code.name == "ERROR"
        assert "ZEBRA" not in json.dumps([dict(s.attributes) for s in exporter.get_finished_spans()])

    def test_a_failed_model_run_is_marked_on_its_span(self, world, exporter, model):
        model.status = "failed"
        run_draft(world, requirements=[{"id": "R1", "text": "one"}])
        llm = by_name(exporter)["llm.run"][0].attributes
        assert llm["llm.status"] == "failed" and llm["outcome"] == "failed"


# ── Never any content ────────────────────────────────────


class TestNoContent:
    def test_no_requirement_prompt_or_answer_text_reaches_any_span(self, world, exporter, model):
        run_draft(world, approach="map_reduce")
        everything = json.dumps(
            [
                {"name": s.name, "attributes": dict(s.attributes), "events": [e.name for e in s.events], "status": str(s.status)}
                for s in exporter.get_finished_spans()
            ]
        )
        for secret in ("ZEBRA", "CONFIDENTIAL", "internal memo", "AES-256", "vendor shall", "Support is available"):
            assert secret not in everything, secret

    def test_every_attribute_name_on_every_span_is_allowed(self, world, exporter, model):
        run_draft(world, approach="map_reduce")
        names = {key for s in exporter.get_finished_spans() for key in s.attributes}
        assert names <= spans.ALLOWED

    def test_free_text_cannot_be_attached_even_by_mistake(self):
        recorded = spans.safe_attributes(
            {
                "prompt": "the whole prompt",
                "answer.chars": 120,
                "requirement.id": "a long sentence that a user pasted from a confidential document",
                "llm.model": "gpt-4o",
                "outcome": "ok",
                "web_search": False,
                "error.type": None,
                "kb.mode": "hybrid+semantic",
                "llm.status": {"nested": "dict"},
            }
        )
        assert recorded == {"answer.chars": 120, "llm.model": "gpt-4o", "outcome": "ok", "web_search": False, "kb.mode": "hybrid+semantic"}

    def test_a_requirement_id_that_is_really_text_is_dropped(self):
        assert spans.safe_attributes({"requirement.id": "Must support SSO with SAML and OIDC for all users"}) == {}
        assert spans.safe_attributes({"requirement.id": "SV-1"}) == {"requirement.id": "SV-1"}
        assert spans.safe_attributes({"requirement.id": "3.1.2"}) == {"requirement.id": "3.1.2"}

    def test_an_exception_is_recorded_by_type_and_its_message_is_left_out(self, exporter):
        with pytest.raises(ValueError):
            with spans.stage("llm.run"):
                raise ValueError(f"model said: {SECRET_ANSWER}")

        (span,) = exporter.get_finished_spans()
        assert span.attributes["error.type"] == "ValueError" and span.attributes["outcome"] == "error"
        assert span.events == ()
        assert "ZEBRA" not in json.dumps(dict(span.attributes)) + str(span.status)

    def test_annotating_without_a_span_is_harmless(self):
        spans.annotate(figures__removed=2, prompt="text")  # no current span: nothing happens, nothing raises


# ── Cost ─────────────────────────────────────────────────


class TestCost:
    def test_cost_is_tokens_times_price_per_million(self):
        assert spans.cost_usd(1_000_000, 0, 2.5, 10.0) == 2.5
        assert spans.cost_usd(1000, 200, 2.5, 10.0) == pytest.approx(0.0045)
        assert spans.cost_usd(0, 0, 2.5, 10.0) == 0.0

    def test_the_meter_adds_up_calls_from_several_threads(self):
        meter = spans.Meter(input_price=1.0, output_price=1.0)
        threads = [threading.Thread(target=lambda: [meter.add_usage(10, 5) for _ in range(100)]) for _ in range(4)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert (meter.calls, meter.input_tokens, meter.output_tokens) == (400, 4000, 2000)

    def test_a_call_outside_a_draft_still_works_and_counts_nowhere(self, exporter, model):
        assert spans.current_meter() is None
        runner.run_agent_prompt("agent", "prompt")
        (llm,) = by_name(exporter)["llm.run"]
        assert llm.attributes["llm.cost_usd"] == pytest.approx(0.0045)

    def test_figures_the_app_removes_are_counted_in_the_draft(self, world, exporter, model, monkeypatch):
        # the model's answer holds a figure that is not in the sources
        reply = json.dumps({"requirement_id": "R1", "answer_markdown": "We improved results by 47%.", "meets": "Yes"})
        monkeypatch.setattr(
            runner, "_run_agent_prompt", lambda *a, **k: runner.AgentRunResult(status="completed", text=reply, has_response=True)
        )
        run_draft(world, requirements=[{"id": "R1", "text": "Explain your results."}])
        draft = by_name(exporter)["draft"][0].attributes
        assert draft["figures.removed"] == 1
        assert by_name(exporter)["requirement"][0].attributes["figures.removed"] == 1


# ── No dead code ─────────────────────────────────────────


def test_no_unused_telemetry_or_docx_helpers_remain():
    for name in ("trace_tool_call", "record_redaction_event", "record_token_usage", "get_tracer"):
        assert not hasattr(tracing, name), name
    assert not hasattr(docx_writer, "_replace_placeholder")
