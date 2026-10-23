"""Invented figures are removed from generated text; the prompts say not to invent them in the first place."""

import json

import pytest

from app.agents import answers, grounding, sections
from app.agents.grounding import REMOVED_MARKER, figures_in, remove_unsupported_figures
from app.agents.kb_search import RetrievedContext
from app.agents.prompts import AGENT_INSTRUCTIONS

from .test_draft_endpoints import agent  # noqa: F401  (a fixture)

# A section narrative as the model wrote it for a real run: plausible, uncited, with made-up results.
INVENTED = (
    "Azure offers a comprehensive suite of AI services [R1, R8]. "
    "A notable example includes the deployment of predictive analytics for a retail client, where AI-driven "
    "insights improved inventory turnover by 15% and enhanced customer satisfaction scores by 20% over six months. "
    "In another instance, we implemented a tailored customer engagement solution, which resulted in a 25% increase "
    "in online customer interactions and a 10% uptick in sales conversions [R10, R6]. "
    "We propose a collaborative model [R9]."
)


def doc(content, title="Guide", url="https://kb/guide.pdf"):
    return {"title": title, "content": content, "source_url": url}


class TestFigures:
    @pytest.mark.parametrize(
        "text, expected",
        [
            ("up 15%", {"15%"}),
            ("up 15 percent", {"15%"}),
            ("up 15.0 %", {"15%"}),
            ("$1,500,000 saved", {"$1500000"}),
            ("a 3x gain and 2-fold", {"3x", "2x"}),
            ("over 10+ years with 250 customers", {"10 year", "250 customer"}),
            ("ISO 27001, TLS 1.3 and 24/7 support", set()),
            ("see [R1, R8] in 2024", set()),
        ],
    )
    def test_what_counts_as_a_figure(self, text, expected):
        assert figures_in(text) == expected


class TestRemoval:
    def test_invented_results_are_removed_and_the_rest_is_kept(self):
        cleaned, removed = remove_unsupported_figures(INVENTED, "Azure AI services overview. Governance model.")

        assert removed == ["15%", "20%", "25%", "10%"]
        assert "15%" not in cleaned and "25%" not in cleaned
        assert "Azure offers a comprehensive suite of AI services [R1, R8]." in cleaned
        assert "We propose a collaborative model [R9]." in cleaned
        assert cleaned.count(REMOVED_MARKER) == 1  # adjacent removals share one marker

    def test_a_figure_in_the_sources_is_kept(self):
        sources = "Case study: inventory turnover improved by 15 percent at a retailer."
        cleaned, removed = remove_unsupported_figures("Turnover improved by 15%.", sources)
        assert (cleaned, removed) == ("Turnover improved by 15%.", [])

    def test_a_figure_the_rfp_itself_states_is_kept(self):
        cleaned, removed = remove_unsupported_figures("We guarantee 99.9% availability.", "The vendor must ensure 99.9% uptime")
        assert removed == [] and "99.9%" in cleaned

    def test_text_without_figures_is_untouched(self):
        text = "Line one.\n\nLine two with [R1]."
        assert remove_unsupported_figures(text, "") == (text, [])

    def test_paragraph_breaks_survive(self):
        cleaned, _ = remove_unsupported_figures("Intro text.\n\nWe saved 40% of cost.\n\nClosing.", "")
        assert cleaned == f"Intro text.\n\n{REMOVED_MARKER}\n\nClosing."

    def test_decimals_do_not_split_sentences(self):
        cleaned, removed = remove_unsupported_figures("Uptime is 99.9% in practice. Next sentence.", "")
        assert removed == ["99.9%"]
        assert cleaned == f"{REMOVED_MARKER} Next sentence."


class TestAnswers:
    REPLY = json.dumps(
        {
            "requirement_id": "R1",
            "answer_markdown": "We improved turnover by 15%. Our platform encrypts data with AES-256 [Guide](https://kb/guide.pdf).",
            "meets": "Yes",
            "evidence": [{"title": "Guide", "source_url": "https://kb/guide.pdf", "quote": "Data is encrypted"}],
        }
    )

    def answer(self, agent, context, **kwargs):  # noqa: F811
        agent.reply = lambda _p: self.REPLY
        return answers.answer_single_requirement(
            requirement={"id": "R1", "text": "Encrypt data"},
            context=context,
            tone="professional",
            agent_id="agent-1",
            **kwargs,
        )

    def test_an_invented_figure_is_removed_and_flagged_for_the_reviewer(self, agent):  # noqa: F811
        result = self.answer(agent, RetrievedContext([doc("Data is encrypted with AES-256.")]))

        assert "15%" not in result["answer_markdown"]
        assert REMOVED_MARKER in result["answer_markdown"]
        assert "AES-256" in result["answer_markdown"]
        assert any("15%" in risk for risk in result["risks"])

    def test_the_users_own_context_counts_as_a_source(self, agent):  # noqa: F811
        result = self.answer(
            agent,
            RetrievedContext([doc("Data is encrypted.")]),
            user_context="Our last rollout improved turnover by 15%.",
        )
        assert "15%" in result["answer_markdown"]
        assert not result.get("risks")

    def test_unparseable_output_is_cleaned_too(self, agent):  # noqa: F811
        agent.reply = lambda _p: "We cut costs by 30%."
        result = answers.answer_single_requirement(
            requirement={"id": "R1", "text": "t"}, context=RetrievedContext(), tone="professional", agent_id="agent-1"
        )
        assert "30%" not in result["answer_markdown"]


class TestNarratives:
    def test_a_section_written_from_sources_is_checked_against_them(self, agent):  # noqa: F811
        agent.reply = lambda _p: json.dumps(
            {"section_name": "S", "narrative_markdown": INVENTED, "requirement_ids": ["R1"]}
        )

        narrative = sections.generate_section_narrative(
            section_name="S",
            req_ids=["R1"],
            section_reqs=[{"id": "R1", "text": "Describe your AI services"}],
            context=RetrievedContext([doc("Azure AI services overview.")]),
            tone="professional",
            agent_id="agent-1",
        )

        assert "15%" not in narrative["narrative_markdown"]
        assert REMOVED_MARKER in narrative["narrative_markdown"]
        assert narrative["narrative_markdown"].startswith("Azure offers a comprehensive suite")

    def test_a_section_written_from_answers_may_use_figures_the_answers_have(self, agent):  # noqa: F811
        agent.reply = lambda _p: json.dumps(
            {"section_name": "S", "narrative_markdown": "Uptime is 99.9%. Savings reached 40%.", "requirement_ids": ["R1"]}
        )

        narrative = sections.generate_section_narrative_from_answers(
            section_name="S",
            req_ids=["R1"],
            section_answers=[{"requirement_id": "R1", "requirement_text": "t", "answer_markdown": "We offer 99.9% uptime."}],
            tone="professional",
            agent_id="agent-1",
        )

        assert narrative["narrative_markdown"] == f"Uptime is 99.9%. {REMOVED_MARKER}"

    def test_a_section_that_is_not_json_is_checked_too(self, agent):  # noqa: F811
        agent.reply = lambda _p: "Growth was 80%."
        narrative = sections.generate_section_narrative(
            section_name="S",
            req_ids=["R1"],
            section_reqs=[{"id": "R1", "text": "t"}],
            context=RetrievedContext(),
            tone="professional",
            agent_id="agent-1",
        )
        assert narrative["narrative_markdown"] == REMOVED_MARKER

    def test_both_narrative_prompts_forbid_inventing_facts_and_ask_for_citations(self, agent):  # noqa: F811
        agent.reply = lambda _p: json.dumps({"section_name": "S", "narrative_markdown": "n", "requirement_ids": []})
        sections.generate_section_narrative(
            section_name="S", req_ids=[], section_reqs=[], context=RetrievedContext(), tone="professional",
            agent_id="agent-1",
        )
        sections.generate_section_narrative_from_answers(
            section_name="S", req_ids=[], section_answers=[], tone="professional", agent_id="agent-1"
        )
        for prompt in agent.prompts:
            assert "[title](source_url)" in prompt
            assert "Do NOT invent statistics" in prompt


def test_the_system_prompt_forbids_invented_facts_and_old_product_names():
    assert "No invented facts" in AGENT_INSTRUCTIONS
    assert "Microsoft Entra ID" in AGENT_INSTRUCTIONS


def test_the_helper_leaves_other_fields_alone():
    item = {"answer_markdown": "Saved 12%.", "meets": "Yes", "evidence": [{"quote": "12%"}]}
    removed = grounding.ground_fields(item, ("answer_markdown",), "")
    assert removed == ["12%"]
    assert item["meets"] == "Yes" and item["evidence"] == [{"quote": "12%"}]
