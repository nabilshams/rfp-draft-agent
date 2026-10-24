"""The project views: what the page shows for requirements, answers, versions and history, and that all of it is escaped.

The HTML builders in ``render.js`` are pure functions, so they are run with Node here. The behaviour in a real page
(opening, editing, approving, drafting, following a draft) was exercised with jsdom against the running app; see the
pull request for #31 part 3.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js is not installed")

ATTACK = '<img src=x onerror="alert(1)">'


def render(expression: str):
    script = f"const r = require('./render.js'); console.log(JSON.stringify({expression}))"
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30, cwd=STATIC)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def requirement(**overrides):
    base = {
        "rid": "R1", "text": "The vendor shall encrypt data.", "type": "requirement", "priority": "mandatory",
        "category": "security", "anchor": "¶2", "status": "extracted", "rev": 1, "answer_version": 0, "answer": None,
        "assignee": "", "assignee_name": "",
    }
    return {**base, **overrides}


def answer(**overrides):
    base = {
        "version": 2, "answer_markdown": "AES-256 at rest.", "meets": "Yes", "evidence": [], "assumptions": [], "risks": [],
        "kb_evidence_found": True, "source": "model", "author": "agent", "author_name": "RFP Draft Agent",
        "created_at": "2026-10-22T12:00:00.000000Z",
    }
    return {**base, **overrides}


def block(req, running=False) -> str:
    return render(f"r.projectRequirementHtml({json.dumps(req)}, {json.dumps(running)})")


class TestRequirementBlocks:
    def test_an_unanswered_requirement_shows_its_state_and_offers_to_write_or_draft(self):
        html = block(requirement())
        assert "No answer yet" in html and 'data-action="edit-answer"' in html and ">Write answer<" in html
        assert ">Draft this<" in html and 'data-action="approve"' not in html and 'data-action="versions"' not in html

    def test_an_answered_requirement_shows_who_wrote_it_when_and_the_actions(self):
        html = block(requirement(status="drafted", answer_version=2, rev=3, answer=answer()))
        assert "Version 2" in html and "drafted by the model" in html and "AES-256 at rest." in html
        assert "Drafted" in html and 'data-action="approve"' in html and "Versions (2)" in html and ">Redraft<" in html

    def test_a_person_is_named_and_a_restore_is_noted(self):
        html = block(requirement(status="edited", answer=answer(source="user", author="u1", author_name="Bea", restored_from=1)))
        assert "written by Bea" in html and "restored from version 1" in html

    def test_an_approved_answer_has_no_approve_button(self):
        assert 'data-action="approve"' not in block(requirement(status="approved", answer=answer(), answer_version=2))

    def test_while_a_draft_runs_redrafting_is_disabled(self):
        html = block(requirement(), running=True)
        assert 'disabled title="A draft is running"' in html

    def test_questions_priorities_anchors_and_assignees_show(self):
        html = block(requirement(type="question", priority="preferred", assignee="u1", assignee_name="Bea"))
        assert ">question<" in html and "badge-preferred" in html and "¶2" in html and "Bea" in html

    def test_the_block_carries_its_id_and_revision_for_the_script(self):
        html = block(requirement(rid="3.1-a", rev=7))
        assert 'data-rid="3.1-a"' in html and 'data-rev="7"' in html and 'id="req-block-3.1-a"' in html

    def test_status_labels(self):
        labels = {s: render(f"r.statusBadgeHtml('{s}')") for s in ("extracted", "drafted", "edited", "approved")}
        assert "No answer yet" in labels["extracted"] and "Drafted" in labels["drafted"]
        assert "Edited" in labels["edited"] and "Approved" in labels["approved"]
        assert render("r.statusBadgeHtml('bogus')") == labels["extracted"]  # an unknown status reads as "no answer yet"


class TestEverythingIsEscaped:
    def test_requirement_fields_cannot_inject_markup(self):
        html = block(requirement(
            rid='R1"><script>x</script>', text=ATTACK, category=ATTACK, anchor=ATTACK, assignee="u", assignee_name=ATTACK,
            answer=answer(answer_markdown=ATTACK, risks=[ATTACK], assumptions=[ATTACK], author_name=ATTACK, source="user"),
            status="edited", answer_version=1,
        ))
        assert "<img" not in html and "<script" not in html and 'onerror="' not in html
        assert "&lt;img" in html

    def test_evidence_text_and_unsafe_links_are_neutralised(self):
        evidence = [
            {"title": ATTACK, "source_url": "javascript:alert(1)", "quote": ATTACK, "source_type": ATTACK, "section": ATTACK},
            {"title": "ok", "source_url": "/kb/doc/abc", "quote": "q", "page": 4},
        ]
        html = block(requirement(status="drafted", answer_version=1, answer=answer(evidence=evidence)))
        assert "javascript:" not in html and "<img" not in html
        assert "/kb/doc/abc#page=4" in html  # a knowledge-base link opens at the cited page

    def test_the_edit_form_escapes_the_current_text(self):
        html = render(f"r.editFormHtml({json.dumps(requirement(rid='R1', answer=answer(answer_markdown='</textarea><b>x')))})")
        assert "</textarea><b>" not in html and "&lt;/textarea&gt;" in html

    def test_versions_and_history_escape_names_text_and_ids(self):
        versions = [answer(version=2, answer_markdown=ATTACK, author_name=ATTACK, source="user"), answer(version=1)]
        html = render(f"r.versionsHtml('R1', {json.dumps(versions)}, 2, 5)")
        assert "<img" not in html and html.count('data-action="restore"') == 1 and "current" in html
        events = [{"at": "2026-10-22T12:00:00Z", "actor": ATTACK, "actor_name": ATTACK, "action": "weird<script>", "rid": ATTACK, "details": {}}]
        html = render(f"r.eventsHtml({json.dumps(events)}, null)")
        assert "<img" not in html and "<script" not in html

    def test_projects_in_the_list_are_escaped(self):
        projects = [{"id": 'x"><script>', "name": ATTACK, "owner": "u", "owner_name": ATTACK, "groups": ["g"], "requirement_count": 3, "created_at": "2026-10-22T12:00:00Z"}]
        html = render(f"r.projectListHtml({json.dumps(projects)})")
        assert "<img" not in html and "<script" not in html and "shared with 1 group(s)" in html


class TestEventsAndLists:
    def test_events_read_as_sentences_that_never_include_answer_text(self):
        cases = {
            "project.created": ({"requirements": 12}, "created the project with 12 requirements"),
            "requirement.updated": ({"changes": {"priority": {}, "category": {}}}, "changed priority, category"),
            "answer.added": ({"version": 3, "source": "model"}, "added answer version 3 (drafted by the model)"),
            "draft.started": ({"requirements": 5}, "started a draft of 5 requirements"),
            "draft.finished": ({"outcome": "cancelled", "saved": 2, "changed_meanwhile": 1, "failed": 1}, "draft cancelled: 2 saved, 1 changed meanwhile, 1 failed"),
        }
        for action, (details, expected) in cases.items():
            assert render(f"r.describeEvent({json.dumps({'action': action, 'details': details})})") == expected
        restored = render("r.describeEvent({action: 'answer.added', details: {version: 4, restored_from: 1, source: 'user'}})")
        assert restored == "restored version 1 as answer version 4"

    def test_the_activity_view_names_who_and_when(self):
        events = [{"at": "2026-10-22T12:00:00Z", "actor": "u1", "actor_name": "Bea", "action": "draft.started", "rid": None, "details": {"requirements": 2}}]
        html = render(f"r.eventsHtml({json.dumps(events)}, null)")
        assert "Bea" in html and "started a draft of 2 requirements" in html and "Recent activity" in html

    def test_an_empty_history_and_an_empty_project_list_say_so(self):
        assert "Nothing recorded yet" in render("r.eventsHtml([], 'R1')")
        assert "No projects yet" in render("r.projectListHtml([])")
        assert "No versions" in render("r.versionsHtml('R1', [], 0, 1)")

    def test_counts_are_shown_by_status(self):
        html = render("r.countsHtml({extracted: 4, drafted: 3, edited: 2, approved: 1}, 10)")
        for text in ("4", "no answer", "drafted", "edited", "approved", "of 10"):
            assert text in html

    def test_the_list_offers_to_open_each_project(self):
        html = render("r.projectListHtml([{id: 'abc', name: 'Bid', owner: 'u', groups: [], requirement_count: 2, created_at: '2026-10-22T12:00:00Z'}])")
        assert 'data-action="open-project" data-project="abc"' in html and "private" in html

class TestAnswerStates:
    PLACEHOLDER = "[SME INPUT REQUIRED: no source in the knowledge base covers this requirement]"

    def placeholder(self, **overrides):
        return answer(meets="", answer_markdown=self.PLACEHOLDER, state="no_evidence", missing="a reference", **overrides)

    def test_a_placeholder_makes_no_compliance_claim_and_is_set_apart(self):
        html = block(requirement(status="needs_input", state="no_evidence", answer_version=1, answer=self.placeholder()))
        assert "Not assessed" in html and ">No<" not in html
        assert "SME INPUT REQUIRED" in html and "dashed" in html
        assert "Needs input" in html and "No evidence" in html

    def test_a_placeholder_cannot_be_approved_but_can_be_written(self):
        html = block(requirement(status="needs_input", state="no_evidence", answer_version=1, answer=self.placeholder()))
        assert 'data-action="approve"' not in html and 'data-action="edit-answer"' in html

    def test_editing_a_placeholder_does_not_default_to_a_compliance_value(self):
        html = render("r.editFormHtml(" + json.dumps(requirement(answer=self.placeholder(), answer_version=1)) + ")")
        assert '<option value="" selected>Not assessed</option>' in html

    def test_a_partial_answer_shows_what_is_missing_and_can_be_approved(self):
        partial = answer(meets="Partial", state="partially_answered", missing="the failover time")
        html = block(requirement(status="drafted", state="partially_answered", answer_version=1, answer=partial))
        assert "Partially answered" in html and "the failover time" in html and 'data-action="approve"' in html

    def test_conflicting_evidence_cannot_be_approved_until_a_person_resolves_it(self):
        conflict = answer(state="conflicting_evidence", missing="the 2022 and 2026 policies differ")
        html = block(requirement(status="drafted", state="conflicting_evidence", answer_version=1, answer=conflict))
        assert "Conflicting evidence" in html and 'data-action="approve"' not in html

    def test_a_failed_requirement_says_so_and_offers_a_retry(self):
        html = block(requirement(state="failed"))
        assert "Drafting failed" in html and ">Draft this<" in html

    def test_an_answer_that_is_fully_supported_shows_that(self):
        html = block(requirement(status="drafted", state="fully_answered", answer_version=1,
                                 answer=answer(state="fully_answered")))
        assert "Fully answered" in html and ">Yes<" in html

    def test_the_state_text_is_escaped(self):
        html = block(requirement(status="drafted", answer_version=1, answer=answer(missing=ATTACK)))
        assert ATTACK not in html and "&lt;img" in html

    def test_counts_include_the_requirements_that_need_input(self):
        html = render("r.countsHtml({extracted: 1, needs_input: 3}, 4)")
        assert "needs input" in html and "<strong>3</strong>" in html

class TestItemTypes:
    def test_a_requirement_shows_what_it_is_how_it_is_answered_and_what_it_asks_for(self):
        html = block(requirement(item_kind="optional", response_type="table", evidence_required=True, implicit=True))
        assert ">optional<" in html and ">table<" in html and ">evidence<" in html and ">inferred<" in html
        plain = block(requirement(item_kind="mandatory"))  # a must is the ordinary case: no kind badge beside the priority
        assert plain.count(">mandatory<") == 1

    def test_a_question_and_a_yes_no_answer(self):
        html = block(requirement(type="question", item_kind="question", response_type="yes_no"))
        assert ">question<" in html and ">yes / no<" in html

    def test_an_old_project_with_untyped_items_looks_as_it_did(self):
        html = block(requirement())
        assert "Not answered" not in html and 'data-action="edit-answer"' in html

    def test_an_instruction_is_not_answered_and_says_so_and_offers_to_change_its_type(self):
        html = block(requirement(item_kind="instruction", response_type="narrative"))
        assert ">instruction<" in html and "Not answered: this is an instruction" in html
        assert 'data-action="edit-type"' in html and 'data-action="edit-answer"' not in html
        assert 'data-action="redraft"' not in html and "No answer yet" not in html and ">Not answered<" in html

    @pytest.mark.parametrize("kind, words", [("evaluation_criterion", "an evaluation criterion"), ("deadline", "a deadline"),
                                              ("submission_rule", "a submission rule")])
    def test_each_kind_that_is_not_answered_is_named_properly(self, kind, words):
        assert f"this is {words}" in block(requirement(item_kind=kind))

    def test_a_repeat_says_which_it_repeats_and_offers_to_answer_it_separately(self):
        html = block(requirement(rid="R3", duplicate_of="R1", status="drafted", answer_version=1,
                                 answer=answer(reused_from="R1")))
        assert ">same as R1<" in html and "The same question as <strong>R1</strong>" in html
        assert 'data-action="ungroup"' in html and "AES-256 at rest." in html  # it shows the shared answer

    def test_the_form_offers_the_lists_and_the_current_values(self):
        html = render("r.itemTypeFormHtml(" + json.dumps(requirement(item_kind="optional", response_type="table",
                                                                     evidence_required=True, duplicate_of="R9")) + ")")
        assert '<option value="optional" selected>optional</option>' in html and '<option value="table" selected>table</option>' in html
        assert 'class="type-evidence" checked' in html and 'value="R9"' in html and 'data-action="save-type"' in html
        for kind in ("question", "mandatory", "instruction", "evaluation_criterion", "deadline", "submission_rule"):
            assert f'value="{kind}"' in html

    def test_everything_about_an_item_is_escaped(self):
        html = block(requirement(duplicate_of=ATTACK, item_kind="x" + ATTACK, response_type=ATTACK))
        form = render("r.itemTypeFormHtml(" + json.dumps(requirement(duplicate_of=ATTACK, response_type=ATTACK)) + ")")
        assert ATTACK not in html and ATTACK not in form and "&lt;img" in html + form

    def test_the_counts_name_what_is_not_answered_and_the_repeats(self):
        html = render("r.countsHtml({extracted: 2, not_answered: 3, repeats: 1}, 6)")
        assert "not answered" in html and "repeats" in html and "<strong>3</strong>" in html
        assert "repeats" not in render("r.countsHtml({extracted: 2}, 2)")


class TestProjectRecord:
    PROJECT = {
        "deadlines": [{"text": "Responses must be submitted by 30 November 2026.", "date": "2026-11-30", "anchor": "¶14"}],
        "submission_rules": ["One electronic copy in PDF format shall be uploaded."],
        "extraction": {"method": "ai", "confirmed": True, "warnings": ["1 item(s) the model listed were not found in the document and were dropped."],
                       "coverage": {"chunks": 3}},
    }

    def record(self, project):
        return render("r.projectRecordHtml(" + json.dumps(project) + ")")

    def test_the_dates_the_rules_the_warnings_and_how_much_was_read_are_shown(self):
        html = self.record(self.PROJECT)
        assert "2026-11-30" in html and "Responses must be submitted" in html and "¶14" in html
        assert "How to submit" in html and "One electronic copy" in html
        assert "were not found in the document" in html and "read all 3 part(s)" in html
        assert "Confirm the requirements" not in html

    def test_a_model_read_project_asks_for_review_before_drafting(self):
        html = self.record({**self.PROJECT, "extraction": {**self.PROJECT["extraction"], "confirmed": False}})
        assert "Review what the model extracted" in html and 'data-action="confirm-extraction"' in html
        assert "cannot be drafted until you do" in html

    def test_a_date_that_could_not_be_read_says_so(self):
        assert "no date found" in self.record({"deadlines": [{"text": "Closing soon.", "date": ""}]})

    def test_a_project_with_nothing_to_show_shows_nothing(self):
        assert self.record({"deadlines": [], "submission_rules": [], "extraction": {"method": "local", "confirmed": True}}) == ""
        assert self.record(None) == ""

    def test_it_is_escaped(self):
        html = self.record({"deadlines": [{"text": ATTACK, "date": ATTACK, "anchor": ATTACK}], "submission_rules": [ATTACK],
                            "extraction": {"confirmed": True, "warnings": [ATTACK]}})
        assert ATTACK not in html and "&lt;img" in html
