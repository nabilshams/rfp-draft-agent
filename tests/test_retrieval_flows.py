"""Tests that retrieval and citation checks are wired into every generation path."""

import json
from types import SimpleNamespace

import pytest
from azure.core.exceptions import HttpResponseError

from app.agents import answers, kb_search, lifecycle, runner, sections, strategies
from app.agents.kb_search import RetrievedContext

KB_A = "https://kb.example/knowledge-base/acme/p1/general/encryption.pdf"
KB_B = "https://kb.example/knowledge-base/acme/p1/general/support.pdf"
BING = "https://azure.microsoft.com/support/legal/sla/"
FAKE = "https://evil.example/invented"

ACCESS = {"client_id": "acme", "project_id": "p1", "solution_area": "general", "user_oid": "user-a", "user_groups": ["t1"]}


def doc(title, url, content=None):
    return {"title": title, "source_url": url, "content": content or f"content of {title}", "score": 1.0}


@pytest.fixture
def agent(monkeypatch):
    """A scripted agent: records prompts, answers with whatever `respond(prompt)` returns."""
    state = SimpleNamespace(
        prompts=[], web_search=[], annotation_urls=set(), respond=lambda prompt: "{}", status="completed"
    )

    def fake_run(agent_id, content, *, web_search=False):
        state.prompts.append(content)
        state.web_search.append(web_search)
        return runner.AgentRunResult(
            status=state.status,
            run_id="run-1",
            text=state.respond(content),
            annotation_urls=set(state.annotation_urls),
            has_response=True,
        )

    monkeypatch.setattr(runner, "run_agent_prompt", fake_run)
    monkeypatch.setattr(lifecycle, "ensure_agent", lambda name: "agent-1")
    return state


@pytest.fixture
def kb(monkeypatch):
    """Fake retrieval: documents are looked up by the query text."""
    state = SimpleNamespace(docs={}, queries=[], embedded=[], failing=set())

    def fake_embed(queries):
        state.embedded.append(list(queries))
        return [[0.1]] * len(queries)

    def fake_search(*, query, vector=None, **access):
        state.queries.append(query)
        if query in state.failing:
            raise HttpResponseError(message="search unavailable")
        return list(state.docs.get(query, []))

    monkeypatch.setattr(kb_search, "embed_queries", fake_embed)
    monkeypatch.setattr(kb_search, "search_kb", fake_search)
    return state


REQS = [
    {"id": "R1", "text": "data is encrypted at rest"},
    {"id": "R2", "text": "24x7 support is provided"},
]


def answer_json(req_id, *, text=None, evidence=None):
    return json.dumps(
        {
            "requirement_id": req_id,
            "requirement_text": "t",
            "answer_markdown": text or f"We comply. See [guide]({KB_A}) and [invented]({FAKE}).",
            "meets": "Yes",
            "evidence": evidence
            if evidence is not None
            else [
                {"title": "Encryption", "source_url": KB_A, "quote": "AES-256"},
                {"title": "Invented", "source_url": FAKE, "quote": "made up"},
            ],
            "assumptions": [],
            "risks": [],
            "followups": [],
        }
    )


# ── run_draft ────────────────────────────────────────────


class TestRunDraft:
    def draft(self, requirements=REQS, requirements_json=None):
        return answers.run_draft(
            rfp_id="rfp1",
            requirements_json=requirements_json if requirements_json is not None else json.dumps(requirements),
            tone="professional",
            **ACCESS,
        )

    def model_output(self):
        return json.dumps(
            {
                "executive_summary": f"Summary citing [KB]({KB_A}) and [web]({BING}) and [fake]({FAKE}).",
                "solution_overview": "overview",
                "responses": [
                    {"requirement_id": "R1", "answer_markdown": f"See [guide]({KB_A}) and [x]({FAKE})",
                     "meets": "Yes",
                     "evidence": [{"title": "E", "source_url": KB_A, "quote": "q"},
                                  {"title": "Bad", "source_url": FAKE, "quote": "q"}]},
                    {"requirement_id": "R2", "answer_markdown": "no sources", "meets": "Partial", "evidence": []},
                ],
            }
        )

    def test_knowledge_base_links_are_kept_and_invented_ones_removed(self, agent, kb):
        # Regression: the allowed-URL set used to be overwritten with only the web-grounding URLs,
        # which stripped every link to a retrieved knowledge-base document.
        kb.docs = {REQS[0]["text"]: [doc("Encryption", KB_A)]}
        agent.respond = lambda prompt: self.model_output()
        agent.annotation_urls = {BING}

        result = self.draft()

        summary = result["executive_summary"]
        assert f"[KB]({KB_A})" in summary  # retrieved KB source: kept
        assert f"[web]({BING})" in summary  # returned by web grounding: kept
        assert FAKE not in summary and "fake" in summary  # invented: link removed, text kept
        first = result["responses"][0]
        assert f"[guide]({KB_A})" in first["answer_markdown"]
        assert FAKE not in first["answer_markdown"]
        assert [e["source_url"] for e in first["evidence"]] == [KB_A]

    def test_each_requirement_is_searched_with_its_own_text(self, agent, kb):
        kb.docs = {REQS[0]["text"]: [doc("Encryption", KB_A)], REQS[1]["text"]: [doc("Support", KB_B)]}
        agent.respond = lambda prompt: self.model_output()

        self.draft()

        assert kb.queries == [REQS[0]["text"], REQS[1]["text"]]
        assert kb.embedded == [[REQS[0]["text"], REQS[1]["text"]]]  # one batched embedding call
        (prompt,) = agent.prompts
        assert "content of Encryption" in prompt and "content of Support" in prompt

    def test_responses_say_whether_the_knowledge_base_had_evidence(self, agent, kb):
        kb.docs = {REQS[0]["text"]: [doc("Encryption", KB_A)]}  # nothing for R2
        agent.respond = lambda prompt: self.model_output()

        result = self.draft()

        assert [r["kb_evidence_found"] for r in result["responses"]] == [True, False]
        assert result["_meta"]["kb_sources"] == 1

    def test_requirements_that_are_not_a_json_list_still_get_one_search(self, agent, kb):
        agent.respond = lambda prompt: self.model_output()

        self.draft(requirements_json="Section 4: the vendor shall encrypt data")

        assert kb.queries == ["Section 4: the vendor shall encrypt data"]

    def test_citations_are_cleaned_even_when_the_output_is_not_valid_json(self, agent, kb):
        kb.docs = {REQS[0]["text"]: [doc("Encryption", KB_A)]}
        agent.respond = lambda prompt: f"Sorry, prose: [ok]({KB_A}) and [bad]({FAKE})"

        result = self.draft()

        assert result["parse_error"]
        assert f"[ok]({KB_A})" in result["raw_response"]
        assert FAKE not in result["raw_response"]


# ── Per-requirement streaming ────────────────────────────


class TestStreamPerRequirement:
    def stream(self, requirements=REQS):
        return list(strategies.stream_per_requirement(requirements=requirements, tone="professional", **ACCESS))

    def test_each_requirement_is_grounded_on_its_own_documents(self, agent, kb):
        kb.docs = {REQS[0]["text"]: [doc("Encryption", KB_A)], REQS[1]["text"]: [doc("Support", KB_B)]}
        agent.respond = lambda prompt: answer_json("R1" if "Requirement ID: R1" in prompt else "R2")

        results = self.stream()

        first_prompt, second_prompt = agent.prompts
        assert "content of Encryption" in first_prompt and "content of Support" not in first_prompt
        assert "content of Support" in second_prompt and "content of Encryption" not in second_prompt
        assert [r["requirement_id"] for r in results] == ["R1", "R2"]

    def test_no_evidence_is_flagged_and_stated_in_the_prompt(self, agent, kb):
        kb.docs = {REQS[0]["text"]: [doc("Encryption", KB_A)]}
        agent.respond = lambda prompt: answer_json("R1" if "Requirement ID: R1" in prompt else "R2", evidence=[])

        first, second = self.stream()

        assert first["kb_evidence_found"] is True
        assert second["kb_evidence_found"] is False
        assert "No matching documents were found" in agent.prompts[1]
        assert "No matching documents were found" not in agent.prompts[0]

    def test_invented_evidence_and_links_are_removed_from_every_answer(self, agent, kb):
        kb.docs = {REQS[0]["text"]: [doc("Encryption", KB_A)]}
        agent.respond = lambda prompt: answer_json("R1")

        (first, *_) = self.stream(REQS[:1])

        assert [e["source_url"] for e in first["evidence"]] == [KB_A]
        assert FAKE not in first["answer_markdown"] and KB_A in first["answer_markdown"]

    def test_web_grounding_urls_are_allowed(self, agent, kb):
        agent.annotation_urls = {BING}
        agent.respond = lambda prompt: answer_json(
            "R1", text=f"See [SLA]({BING})", evidence=[{"title": "SLA", "source_url": BING, "quote": "99.9%"}]
        )

        (first,) = self.stream(REQS[:1])

        assert BING in first["answer_markdown"]
        assert [e["source_url"] for e in first["evidence"]] == [BING]

    def test_a_failed_search_does_not_stop_the_requirement_being_answered(self, agent, kb):
        kb.docs = {REQS[1]["text"]: [doc("Support", KB_B)]}
        kb.failing = {REQS[0]["text"]}
        agent.respond = lambda prompt: answer_json("R1" if "Requirement ID: R1" in prompt else "R2", evidence=[])

        first, second = self.stream()

        assert first["kb_evidence_found"] is False and "Error" not in first["answer_markdown"]
        assert second["kb_evidence_found"] is True

    def test_repeated_search_failures_are_reported_not_hidden(self, agent, kb):
        requirements = [{"id": f"R{i}", "text": f"req {i}"} for i in range(4)]
        kb.failing = {r["text"] for r in requirements}
        agent.respond = lambda prompt: answer_json("R0", evidence=[])

        results = self.stream(requirements)

        assert [r["kb_evidence_found"] for r in results] == [False] * 4
        assert results[2]["answer_markdown"].startswith("Error:")  # the third consecutive failure is raised
        assert len(agent.prompts) == 2  # requirements 3 and 4 were not sent to the model without evidence


# ── Single answers ───────────────────────────────────────


class TestSingleAnswer:
    def ask(self):
        return answers.run_single_answer(question="Do you encrypt data?", **ACCESS)

    def test_the_question_is_searched_and_the_answer_is_cleaned(self, agent, kb):
        kb.docs = {"Do you encrypt data?": [doc("Encryption", KB_A)]}
        agent.respond = lambda prompt: json.dumps(
            {
                "answer_markdown": f"Yes, [see]({KB_A}) or [here]({FAKE})",
                "evidence": [
                    {"title": "E", "source_url": KB_A, "quote": "q"},
                    {"title": "Bad", "source_url": FAKE, "quote": "q"},
                ],
            }
        )

        answer = self.ask()

        assert kb.embedded == [["Do you encrypt data?"]]
        assert "content of Encryption" in agent.prompts[0]
        assert [e["source_url"] for e in answer["evidence"]] == [KB_A]
        assert FAKE not in answer["answer_markdown"]
        assert answer["kb_evidence_found"] is True

    def test_an_answer_without_sources_says_so(self, agent, kb):
        agent.respond = lambda prompt: json.dumps({"answer_markdown": "Not provided in sources", "evidence": []})
        answer = self.ask()
        assert answer["kb_evidence_found"] is False
        assert "No matching documents were found" in agent.prompts[0]


# ── Section narratives ───────────────────────────────────


def router(agent):
    """Answer each kind of prompt the section approaches send."""

    def respond(prompt):
        if "Classify each requirement" in prompt:
            return json.dumps({"Executive Summary": ["R1"], "Technical Approach": ["R2"]})
        if "Respond to this single RFP requirement" in prompt:
            return answer_json("R1" if "Requirement ID: R1" in prompt else "R2")
        section = "Executive Summary" if '"Executive Summary" section' in prompt else "Technical Approach"
        return json.dumps(
            {
                "section_name": section,
                "narrative_markdown": f"Narrative [kb]({KB_A}) [kb2]({KB_B}) [bad]({FAKE})",
                "requirement_ids": [],
            }
        )

    agent.respond = respond


class TestSections:
    def run(self, approach, kb):
        kb.docs = {REQS[0]["text"]: [doc("Encryption", KB_A)], REQS[1]["text"]: [doc("Support", KB_B)]}
        events = list(
            approach(
                requirements=REQS,
                template_sections=[{"heading": "Executive Summary"}, {"heading": "Technical Approach"}],
                tone="professional",
                **ACCESS,
            )
        )
        return [e["data"] for e in events if e["type"] == "section_narrative"]

    def test_a_section_is_grounded_on_its_own_requirements_documents(self, agent, kb):
        router(agent)

        self.run(strategies.stream_per_section, kb)

        narrative_prompts = [p for p in agent.prompts if "section of an RFP response document" in p]
        by_section = {("Executive" if "Executive Summary" in p.split("\n")[0] else "Technical"): p for p in narrative_prompts}
        assert "content of Encryption" in by_section["Executive"] and "content of Support" not in by_section["Executive"]
        assert "content of Support" in by_section["Technical"] and "content of Encryption" not in by_section["Technical"]

    def test_narratives_cite_only_their_own_sources(self, agent, kb):
        router(agent)

        narratives = {n["section_name"]: n["narrative_markdown"] for n in self.run(strategies.stream_per_section, kb)}

        assert f"[kb]({KB_A})" in narratives["Executive Summary"]
        assert KB_B not in narratives["Executive Summary"]  # retrieved for another section
        assert FAKE not in narratives["Executive Summary"]
        assert f"[kb2]({KB_B})" in narratives["Technical Approach"]

    def test_map_reduce_narratives_cite_only_what_the_answers_cite(self, agent, kb):
        router(agent)

        narratives = self.run(strategies.stream_map_reduce, kb)

        text = " ".join(n["narrative_markdown"] for n in narratives)
        assert f"[kb]({KB_A})" in text  # cited by the requirement answers
        assert FAKE not in text
        assert f"[kb2]({KB_B})" not in text  # never cited in an answer, so not citable in the narrative


def test_a_section_narrative_function_checks_links_against_its_context(agent):
    agent.respond = lambda prompt: json.dumps(
        {"section_name": "S", "narrative_markdown": f"[a]({KB_A}) [b]({FAKE})", "requirement_ids": ["R1"]}
    )

    narrative = sections.generate_section_narrative(
        section_name="S",
        req_ids=["R1"],
        section_reqs=REQS[:1],
        context=RetrievedContext([doc("Encryption", KB_A)]),
        tone="professional",
        agent_id="agent-1",
    )

    assert narrative["narrative_markdown"] == f"[a]({KB_A}) b"
