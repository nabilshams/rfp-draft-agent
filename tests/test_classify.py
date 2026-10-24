"""Requirement model v2: typed fields, repeats, dates and sentence-level extraction (#32)."""

import pytest

from app.ingestion import classify
from app.ingestion.rfp_extract import Requirement, extract_rfp_structure


class TestKind:
    @pytest.mark.parametrize("text, kind", [
        ("The vendor shall encrypt all customer data.", "mandatory"),
        ("The vendor must hold ISO 27001.", "mandatory"),
        ("What is your typical implementation timeline?", "question"),
        ("The vendor should provide training.", "optional"),
        ("Vendors may offer a managed service.", "optional"),
        ("Preferably hosted in Australia.", "optional"),
        ("Each answer shall be no longer than 200 words.", "instruction"),
        ("Responses must not exceed 20 pages.", "instruction"),
        ("Use 11 point font and number each answer.", "instruction"),
        ("Answers must be written in English.", "instruction"),
        ("Responses must be submitted by 30 November 2026.", "deadline"),
        ("The closing date is 2026-12-01.", "deadline"),
        ("Submit one electronic copy to the tender portal.", "submission_rule"),
        ("Hard copies shall be delivered to the Council office.", "submission_rule"),
        ("Proposals will be scored on price (30 percent).", "evaluation_criterion"),
        ("The platform shall be delivered as a service.", "mandatory"),  # "deliver" in a requirement about the product
        ("The system must submit audit events to the SIEM.", "mandatory"),  # "submit" about the product
    ])
    def test_the_kind_comes_from_the_wording(self, text, kind):
        assert classify.kind_of(text) == kind

    def test_a_question_stays_a_question_whatever_else_it_says(self):
        assert classify.kind_of("Must responses be submitted by 30 November 2026?") == "question"

    def test_a_sheets_priority_decides_between_mandatory_and_optional(self):
        assert classify.kind_of("Provide online training.", "preferred") == "optional"
        assert classify.kind_of("Provide online training.", "mandatory") == "mandatory"
        assert classify.kind_of("Provide online training.") == "mandatory"  # unstated: answered, priority says so

    def test_only_three_kinds_are_answered(self):
        assert set(classify.ANSWERED_KINDS) == {"question", "mandatory", "optional"}


class TestResponseTypeAndEvidence:
    @pytest.mark.parametrize("text, kind, expected", [
        ("Does your solution include an on-premises appliance?", "question", "yes_no"),
        ("Can you support SAML 2.0?", "question", "yes_no"),
        ("How will staff sign in?", "question", "narrative"),
        ("Describe your approach to implementation.", "mandatory", "narrative"),
        ("Provide your rates in a table.", "mandatory", "table"),
        ("Complete the pricing schedule.", "mandatory", "table"),
        ("Attach a copy of your insurance certificate.", "mandatory", "attachment"),
        ("Include your CV as an appendix.", "mandatory", "attachment"),
        ("Each answer shall be no longer than 200 words.", "instruction", "narrative"),
    ])
    def test_how_an_item_is_answered(self, text, kind, expected):
        assert classify.response_type_of(text, kind) == expected

    @pytest.mark.parametrize("text, expected", [
        ("The vendor shall provide a SOC 2 Type II report.", True),
        ("The vendor must supply three customer references.", True),
        ("Provide a case study of a similar migration.", True),
        ("Provide evidence of your accreditation.", True),
        ("The vendor shall encrypt data at rest.", False),
        ("Describe your support hours.", False),
    ])
    def test_whether_evidence_is_asked_for(self, text, expected):
        assert classify.evidence_required_of(text, "mandatory") is expected

    def test_an_instruction_asks_for_no_evidence(self):
        assert classify.evidence_required_of("Attach references to the cover page.", "instruction") is False


class TestDates:
    @pytest.mark.parametrize("text, expected", [
        ("Due 30 November 2026.", "2026-11-30"), ("Closes on 1st Dec 2026", "2026-12-01"),
        ("Closing date: November 30, 2026", "2026-11-30"), ("By 2027-01-15 at noon", "2027-01-15"),
        ("No date here.", ""), ("Due 31 February 2026", ""), ("Due 5 Smarch 2026", ""),
    ])
    def test_a_date_is_read_or_it_is_not(self, text, expected):
        assert classify.parse_date(text) == expected


class TestRepeats:
    def items(self, *texts, kinds=None):
        return [{"id": f"R{n}", "text": t, "kind": (kinds or {}).get(n, "mandatory"), "response_type": "narrative"}
                for n, t in enumerate(texts, start=1)]

    def test_a_question_asked_again_points_at_the_first_and_nothing_else_is_grouped(self):
        groups = classify.find_duplicates(self.items(
            "Describe the online training you provide for administrators.",
            "Describe your approach to implementation.",
            "Please describe the online training that you provide for administrators.",
            "Describe your approach to data migration.",
        ))
        assert groups == {"R3": "R1"}

    def test_a_third_copy_points_at_the_first_not_at_the_second(self):
        text = "Describe the online training for administrators and end users."
        assert classify.find_duplicates(self.items(text, text, text)) == {"R2": "R1", "R3": "R1"}

    def test_a_yes_no_and_a_narrative_about_the_same_thing_are_not_merged(self):
        items = self.items("Describe online training for administrators.", "Do you provide online training for administrators?")
        items[1]["response_type"] = "yes_no"
        assert classify.find_duplicates(items) == {}

    def test_items_that_are_not_answered_are_never_grouped(self):
        assert classify.find_duplicates(self.items("Each answer must be short.", "Each answer must be short.",
                                                   kinds={1: "instruction", 2: "instruction"})) == {}

    def test_very_short_items_have_too_little_to_compare(self):
        assert classify.find_duplicates(self.items("Provide SLA.", "Provide SLA.")) == {}
        assert classify.similarity("a b", "a b") == 0.0

    def test_wording_that_differs_in_what_matters_is_not_a_repeat(self):
        assert classify.find_duplicates(self.items(
            "The vendor shall retain backups for 35 days in Australia.",
            "The vendor shall retain backups for 90 days in Australia.")) == {}  # a different number is a different ask


class TestModel:
    def test_every_requirement_has_typed_fields_even_when_built_without_them(self):
        item = Requirement(id="R1", text="Does your solution support SSO?", type="question")
        assert (item.kind, item.type, item.response_type, item.answered) == ("question", "question", "yes_no", True)

    def test_a_stated_type_is_kept_and_a_bad_one_is_replaced(self):
        item = Requirement(id="R1", text="Provide a report.", kind="optional", response_type="table", evidence_required=False)
        assert (item.kind, item.response_type, item.evidence_required) == ("optional", "table", False)
        bad = Requirement(id="R1", text="Provide a report.", kind="nonsense", response_type="essay")
        assert (bad.kind, bad.response_type, bad.evidence_required) == ("mandatory", "narrative", True)

    def test_an_instruction_is_not_answered(self):
        item = Requirement(id="R1", text="Each answer shall be no longer than 200 words.")
        assert item.kind == "instruction" and item.type == "requirement" and not item.answered

    def test_a_sheets_priority_codes_are_read(self):
        assert [Requirement(id="x", text="Provide training.", priority=p).priority for p in ("M", "S", "Critical", "n", "huh")] == [
            "mandatory", "preferred", "mandatory", "standard", "standard"]


class TestSentences:
    def test_a_requirement_keeps_its_subject_and_is_not_cut_at_a_version_number(self):
        text = "The platform shall encrypt data with TLS 1.2 or higher. Responses should be brief."
        (item,) = extract_rfp_structure(text).requirements
        assert item.text == "The platform shall encrypt data with TLS 1.2 or higher"

    def test_every_shall_sentence_on_a_line_is_found_and_each_is_whole(self):
        text = "The vendor shall log all access to records. Tenderers must keep copies of all correspondence for audit."
        assert [r.text for r in extract_rfp_structure(text).requirements] == [
            "The vendor shall log all access to records", "Tenderers must keep copies of all correspondence for audit"]

    def test_an_instruction_and_a_deadline_are_typed_and_collected_not_drafted(self):
        text = ("1. Response format\nEach answer shall be no longer than 200 words.\n2. Submission\n"
                "Responses must be submitted by 30 November 2026 via the tender portal.\n3. Requirements\n"
                "The vendor shall support single sign-on.")
        result = extract_rfp_structure(text)
        kinds = {r.text[:12]: (r.kind, r.answered) for r in result.requirements}
        assert kinds == {"Each answer ": ("instruction", False), "Responses mu": ("deadline", False),
                         "The vendor s": ("mandatory", True)}
        assert result.deadlines[0]["date"] == "2026-11-30" and result.method == "local"

    def test_a_repeated_requirement_is_grouped_by_id(self):
        text = "The vendor shall provide online training for administrators.\nThe vendor shall provide online training for administrators."
        first, second = extract_rfp_structure(text).requirements
        assert second.duplicate_of == first.id and first.duplicate_of == ""

    def test_a_reference_to_an_item_that_is_not_there_is_dropped(self):
        from app.ingestion.rfp_extract import RFPExtractionResult, finalise

        result = finalise(RFPExtractionResult(requirements=[Requirement(id="R1", text="Provide training for users.", duplicate_of="R9")]))
        assert result.requirements[0].duplicate_of == ""

    def test_a_chain_of_repeats_points_at_the_first(self):
        from app.ingestion.rfp_extract import RFPExtractionResult, finalise

        result = finalise(RFPExtractionResult(requirements=[
            Requirement(id="A", text="Describe your training for administrators."),
            Requirement(id="B", text="Describe your other thing entirely here.", duplicate_of="A"),
            Requirement(id="C", text="Describe your third thing entirely different.", duplicate_of="B")]))
        assert [r.duplicate_of for r in result.requirements] == ["", "A", "A"]
