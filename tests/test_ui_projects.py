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
