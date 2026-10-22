"""The project ledger API: durable state, sharing, attribution, versions, concurrency and the audit trail."""

import pytest

from app.ledger import store as ledger_store
from app.ledger.store import replace

from .test_authorization_api import ADMIN, A, B, C, upload_rfp
from .test_extraction_fidelity import make_docx

REQUIREMENTS = [
    {"id": "R1", "text": "The vendor shall encrypt data at rest.", "priority": "mandatory", "anchor": "¶2"},
    {"id": "R2", "text": "How do you handle data residency?", "type": "question", "anchor": "¶3"},
    {"id": "R3", "text": "The vendor must provide 24/7 support.", "priority": "mandatory", "anchor": "¶4"},
]


@pytest.fixture
def api(client, use_settings):
    use_settings("easyauth", ledger_backend="memory")
    return client


async def make_project(api, who=A, name="Harbourview bid", requirements=REQUIREMENTS, **extra):
    response = await api.post("/projects", json={"name": name, "requirements": requirements, **extra}, headers=who)
    assert response.status_code == 201, response.text
    return response.json()


def url(project, *parts):
    return "/".join(["/projects", project["project"]["id"], *parts])


# ── Creating and reloading ───────────────────────────────


class TestCreate:
    async def test_a_project_from_an_uploaded_rfp_holds_its_extracted_requirements(self, api):
        rfp_id = await upload_rfp(api, A)

        response = await api.post("/projects", json={"name": "Sample", "rfp_id": rfp_id}, headers=A)

        assert response.status_code == 201, response.text
        state = response.json()
        assert state["project"]["owner"] == "user-a" and state["project"]["rfp_id"] == rfp_id
        requirements = state["requirements"]
        assert len(requirements) >= 6  # four "shall/must" statements and two questions in the sample RFP
        assert [r["order"] for r in requirements] == sorted(r["order"] for r in requirements)
        assert all(r["anchor"] and r["status"] == "extracted" and r["rev"] == 1 for r in requirements)
        assert {r["type"] for r in requirements} == {"requirement", "question"}
        assert len({r["rid"] for r in requirements}) == len(requirements)

    async def test_reloading_restores_the_project_requirements_and_answers(self, api):
        project = await make_project(api)
        await api.post(url(project, "requirements/R1/answers"), json={"answer_markdown": "AES-256."}, headers=A)

        reloaded = (await api.get(url(project), headers=A)).json()

        assert [r["rid"] for r in reloaded["requirements"]] == ["R1", "R2", "R3"]
        assert reloaded["requirements"][0]["answer"]["answer_markdown"] == "AES-256."
        assert reloaded["counts"] == {"extracted": 2, "drafted": 0, "edited": 1, "approved": 0}

    async def test_the_rfps_guidance_is_kept_with_the_project_for_drafting(self, api):
        def build(document):
            for line in ["1. Scope", "The vendor shall host data in Australia.", "2. Evaluation Criteria",
                         "Technical approach 40 percent, price 60 percent."]:
                document.add_paragraph(line)

        rfp_id = await upload_rfp(api, A, make_docx(build), "rfp.docx")
        state = (await api.post("/projects", json={"name": "G", "rfp_id": rfp_id}, headers=A)).json()

        stored = ledger_store.store_for(api_settings()).get(state["project"]["id"], "project")
        assert "price 60 percent" in stored["guidance"]
        assert "guidance" not in state["project"]  # not part of what the UI loads

    async def test_ids_are_cleaned_and_made_unique(self, api):
        rows = [{"id": "3.1/a b", "text": "first requirement text"}, {"id": "3.1/a b", "text": "second requirement text"},
                {"id": "", "text": "third requirement text"}, {"text": "fourth requirement text"}]
        state = await make_project(api, requirements=rows)
        assert [r["rid"] for r in state["requirements"]] == ["3.1-a-b", "3.1-a-b-2", "R3", "R4"]

    async def test_supplied_text_is_redacted_and_blank_rows_are_skipped(self, api):
        rows = [{"id": "R1", "text": "Contact jane@example.com about the $50,000 budget"}, {"id": "R2", "text": "   "}]
        state = await make_project(api, requirements=rows)
        assert [r["text"] for r in state["requirements"]] == ["Contact [REDACTED-EMAIL] about the [REDACTED-PRICE] budget"]

    async def test_a_project_needs_a_source_and_at_least_one_requirement(self, api):
        assert (await api.post("/projects", json={"name": "x"}, headers=A)).status_code == 422
        empty = await api.post("/projects", json={"name": "x", "requirements": [{"text": " "}]}, headers=A)
        assert empty.status_code == 422

    async def test_a_very_large_rfp_is_refused_with_the_limit(self, api, use_settings):
        use_settings("easyauth", ledger_backend="memory", ledger_max_requirements=3)
        rows = [{"text": f"requirement number {i}"} for i in range(4)]
        response = await api.post("/projects", json={"name": "big", "requirements": rows}, headers=A)
        assert response.status_code == 422 and "at most 3" in response.json()["detail"]

    async def test_many_requirements_are_stored_across_several_batches(self, api):
        rows = [{"id": f"R{i}", "text": f"requirement number {i}"} for i in range(1, 251)]
        state = await make_project(api, requirements=rows)
        assert len(state["requirements"]) == 250
        assert (await api.get(url(state), headers=A)).json()["requirements"][249]["rid"] == "R250"

    async def test_a_failure_part_way_leaves_no_half_project(self, api, monkeypatch):
        store = ledger_store.store_for(api_settings())
        real = store.apply
        calls = []

        def failing(project_id, ops):
            calls.append(len(ops))
            if len(calls) == 2:
                raise RuntimeError("the database went away")
            return real(project_id, ops)

        monkeypatch.setattr(store, "apply", failing)
        rows = [{"id": f"R{i}", "text": f"requirement number {i}"} for i in range(1, 201)]

        with pytest.raises(RuntimeError):
            await api.post("/projects", json={"name": "x", "requirements": rows}, headers=A)

        assert store.list_projects(owner="user-a", groups=[], everything=True) == []

    async def test_an_rfp_that_is_not_yours_cannot_be_used(self, api):
        rfp_id = await upload_rfp(api, A)
        assert (await api.post("/projects", json={"name": "x", "rfp_id": rfp_id}, headers=B)).status_code == 404

    async def test_unknown_fields_are_refused(self, api):
        response = await api.post("/projects", json={"name": "x", "requirements": REQUIREMENTS, "owner": "user-z"}, headers=A)
        assert response.status_code == 422


# ── Who can open a project ───────────────────────────────


class TestAccess:
    async def test_a_private_project_is_invisible_to_everyone_else(self, api):
        project = await make_project(api)
        for who in (B, C):
            assert (await api.get(url(project), headers=who)).status_code == 404
            assert (await api.get(url(project, "events"), headers=who)).status_code == 404
            assert (await api.patch(url(project, "requirements/R1"), json={"status": "edited"}, headers=who)).status_code == 404
            assert (await api.post(url(project, "requirements/R1/answers"), json={"answer_markdown": "x"}, headers=who)).status_code == 404
            assert (await api.delete(url(project), headers=who)).status_code == 404
        assert (await api.get("/projects", headers=B)).json()["projects"] == []

    async def test_a_second_user_in_a_shared_group_sees_the_same_state(self, api):
        project = await make_project(api, share_with_groups=["team-1"])
        await api.post(url(project, "requirements/R1/answers"), json={"answer_markdown": "AES-256."}, headers=A)

        seen_by_b = (await api.get(url(project), headers=B)).json()

        assert seen_by_b["requirements"][0]["answer"]["answer_markdown"] == "AES-256."
        assert [p["id"] for p in (await api.get("/projects", headers=B)).json()["projects"]] == [project["project"]["id"]]
        assert (await api.get(url(project), headers=C)).status_code == 404  # another group: still hidden

    async def test_an_administrator_can_open_any_project(self, api):
        project = await make_project(api)
        assert (await api.get(url(project), headers=ADMIN)).status_code == 200
        assert len((await api.get("/projects", headers=ADMIN)).json()["projects"]) == 1

    async def test_the_list_only_shows_what_you_may_open_newest_first(self, api):
        first = await make_project(api, name="first")
        second = await make_project(api, name="second")
        await make_project(api, who=C, name="someone else's")
        names = [p["name"] for p in (await api.get("/projects", headers=A)).json()["projects"]]
        assert names == ["second", "first"] and first and second

    async def test_only_the_owner_can_rename_share_or_delete(self, api):
        project = await make_project(api, share_with_groups=["team-1"])
        assert (await api.patch(url(project), json={"name": "mine now"}, headers=B)).status_code == 403
        assert (await api.patch(url(project), json={"share_with_groups": []}, headers=B)).status_code == 403
        assert (await api.delete(url(project), headers=B)).status_code == 403
        assert (await api.get(url(project), headers=B)).status_code == 200  # still there

        renamed = await api.patch(url(project), json={"name": "Renamed", "share_with_groups": []}, headers=A)
        assert renamed.status_code == 200 and renamed.json()["name"] == "Renamed"
        assert (await api.get(url(project), headers=B)).status_code == 404  # no longer shared

    async def test_a_project_can_only_be_shared_with_groups_you_belong_to(self, api):
        response = await api.post("/projects", json={"name": "x", "requirements": REQUIREMENTS, "share_with_groups": ["team-9"]}, headers=A)
        assert response.status_code == 403
        assert (await api.get("/projects", headers=A)).json()["projects"] == []

    async def test_deleting_removes_everything_including_the_history(self, api):
        project = await make_project(api)
        await api.post(url(project, "requirements/R1/answers"), json={"answer_markdown": "x"}, headers=A)
        deleted = await api.delete(url(project), headers=A)
        assert deleted.status_code == 200 and deleted.json()["deleted_documents"] > 4
        assert (await api.get(url(project), headers=A)).status_code == 404
        assert ledger_store.store_for(api_settings()).query(project["project"]["id"], "event") == []

    async def test_malformed_ids_are_rejected_before_any_lookup(self, api):
        assert (await api.get("/projects/not-an-id", headers=A)).status_code == 422
        project = await make_project(api)
        assert (await api.get(url(project, "requirements/..%2Fx"), headers=A)).status_code in (404, 422)
        assert (await api.get(url(project, "requirements/R1"), headers=A)).status_code == 200
        assert (await api.get(url(project, "requirements/NOPE"), headers=A)).status_code == 404

    async def test_without_a_configured_ledger_the_endpoints_say_so(self, client, use_settings):
        use_settings("easyauth")  # no Cosmos endpoint, no backend
        response = await client.get("/projects", headers=A)
        assert response.status_code == 503 and "COSMOS_ENDPOINT" in response.json()["detail"]


# ── Editing, attribution and the audit trail ─────────────


class TestEditing:
    async def test_an_edit_is_attributed_timestamped_and_recorded(self, api):
        project = await make_project(api, share_with_groups=["team-1"])

        edited = await api.patch(
            url(project, "requirements/R1"),
            json={"priority": "preferred", "assignee": "user-b", "assignee_name": "Bea", "category": "security"},
            headers=B,
        )

        assert edited.status_code == 200, edited.text
        body = edited.json()
        assert (body["priority"], body["assignee"], body["assignee_name"], body["category"]) == ("preferred", "user-b", "Bea", "security")
        assert body["updated_by"] == "user-b" and body["rev"] == 2 and body["updated_at"] > body["created_at"]
        (event,) = (await api.get(url(project, "events"), params={"rid": "R1"}, headers=A)).json()["events"][:1]
        assert (event["actor"], event["action"], event["rid"]) == ("user-b", "requirement.updated", "R1")
        assert event["details"]["changes"]["priority"] == {"from": "mandatory", "to": "preferred"}

    async def test_a_change_that_changes_nothing_writes_nothing(self, api):
        project = await make_project(api)
        again = await api.patch(url(project, "requirements/R1"), json={"priority": "mandatory"}, headers=A)
        assert again.json()["rev"] == 1
        events = (await api.get(url(project, "events"), headers=A)).json()["events"]
        assert [e["action"] for e in events] == ["project.created"]

    async def test_text_edits_are_redacted_and_the_old_text_is_kept_in_the_history(self, api):
        project = await make_project(api)
        await api.patch(url(project, "requirements/R1"), json={"text": "Call 555-123-4567 about encryption"}, headers=A)
        requirement = (await api.get(url(project, "requirements/R1"), headers=A)).json()
        assert "555-123-4567" not in requirement["text"]
        (event,) = (await api.get(url(project, "events"), params={"rid": "R1"}, headers=A)).json()["events"]
        assert event["details"]["changes"]["text"]["from"] == "The vendor shall encrypt data at rest."

    async def test_clearing_an_assignee_clears_the_name_too(self, api):
        project = await make_project(api)
        await api.patch(url(project, "requirements/R1"), json={"assignee": "user-a", "assignee_name": "Al"}, headers=A)
        cleared = (await api.patch(url(project, "requirements/R1"), json={"assignee": ""}, headers=A)).json()
        assert (cleared["assignee"], cleared["assignee_name"]) == ("", "")

    @pytest.mark.parametrize(
        "body",
        [{"status": "finished"}, {"priority": "urgent"}, {"text": ""}, {"assignee": "bad value!"}, {"rev": 5}, {"expected_rev": 0}],
    )
    async def test_invalid_edits_are_refused(self, api, body):
        project = await make_project(api)
        assert (await api.patch(url(project, "requirements/R1"), json=body, headers=A)).status_code == 422

    async def test_a_stale_view_is_refused_so_nobody_overwrites_anybody(self, api):
        project = await make_project(api, share_with_groups=["team-1"])
        await api.patch(url(project, "requirements/R1"), json={"category": "security"}, headers=A)  # rev 2

        late = await api.patch(url(project, "requirements/R1"), json={"category": "other", "expected_rev": 1}, headers=B)

        assert late.status_code == 409 and "Reload" in late.json()["detail"]
        assert (await api.get(url(project, "requirements/R1"), headers=A)).json()["category"] == "security"
        ok = await api.patch(url(project, "requirements/R1"), json={"category": "other", "expected_rev": 2}, headers=B)
        assert ok.status_code == 200

    async def test_a_write_racing_with_another_is_refused_even_without_expected_rev(self, api, monkeypatch):
        project = await make_project(api)
        store = ledger_store.store_for(api_settings())
        real = store.apply

        def racing(project_id, ops):
            monkeypatch.setattr(store, "apply", real)
            sneaky = store.get(project_id, "req:R1")
            real(project_id, [replace({**sneaky, "category": "sneaky"})])  # another writer gets in first
            return real(project_id, ops)

        monkeypatch.setattr(store, "apply", racing)
        response = await api.patch(url(project, "requirements/R1"), json={"priority": "preferred"}, headers=A)
        assert response.status_code == 409
        assert (await api.get(url(project, "requirements/R1"), headers=A)).json()["priority"] == "mandatory"

    async def test_the_audit_trail_lists_newest_first_and_pages(self, api):
        project = await make_project(api)
        for category in ("a", "b", "c"):
            await api.patch(url(project, "requirements/R1"), json={"category": category}, headers=A)

        first = (await api.get(url(project, "events"), params={"limit": 2}, headers=A)).json()
        second = (await api.get(url(project, "events"), params={"limit": 2, "offset": first["next_offset"]}, headers=A)).json()

        assert first["count"] == 2 and second["count"] == 2
        assert first["events"][0]["details"]["changes"]["category"]["to"] == "c"
        assert second["events"][-1]["action"] == "project.created"
        assert all(e["at"] and e["actor"] == "user-a" for e in first["events"] + second["events"])


# ── Answers ──────────────────────────────────────────────


class TestAnswers:
    async def test_answers_keep_every_version(self, api):
        project = await make_project(api, share_with_groups=["team-1"])
        first = await api.post(url(project, "requirements/R1/answers"), json={"answer_markdown": "One.", "meets": "Partial"}, headers=A)
        second = await api.post(url(project, "requirements/R1/answers"), json={"answer_markdown": "Two.", "meets": "Yes"}, headers=B)

        assert first.status_code == second.status_code == 201
        assert first.json()["answer_version"] == 1 and second.json()["answer_version"] == 2
        versions = (await api.get(url(project, "requirements/R1/answers"), headers=A)).json()["answers"]
        assert [(v["version"], v["answer_markdown"], v["author"]) for v in versions] == [(2, "Two.", "user-b"), (1, "One.", "user-a")]
        assert versions[0]["source"] == "user" and versions[0]["created_at"]
        assert (await api.get(url(project, "requirements/R1"), headers=A)).json()["answer"]["answer_markdown"] == "Two."

    async def test_restoring_an_old_version_makes_a_new_one(self, api):
        project = await make_project(api)
        await api.post(url(project, "requirements/R1/answers"), json={"answer_markdown": "One."}, headers=A)
        await api.post(url(project, "requirements/R1/answers"), json={"answer_markdown": "Two."}, headers=A)

        restored = await api.post(url(project, "requirements/R1/answers"), json={"restore_version": 1}, headers=A)

        assert restored.status_code == 201 and restored.json()["answer_version"] == 3
        assert restored.json()["answer"]["answer_markdown"] == "One." and restored.json()["answer"]["restored_from"] == 1
        versions = (await api.get(url(project, "requirements/R1/answers"), headers=A)).json()["answers"]
        assert [v["answer_markdown"] for v in versions] == ["One.", "Two.", "One."]  # history untouched
        assert (await api.post(url(project, "requirements/R1/answers"), json={"restore_version": 9}, headers=A)).status_code == 404

    async def test_a_new_answer_withdraws_an_approval(self, api):
        project = await make_project(api)
        assert (await api.patch(url(project, "requirements/R1"), json={"status": "approved"}, headers=A)).status_code == 409  # nothing to approve
        await api.post(url(project, "requirements/R1/answers"), json={"answer_markdown": "Good."}, headers=A)
        approved = await api.patch(url(project, "requirements/R1"), json={"status": "approved"}, headers=A)
        assert approved.json()["status"] == "approved"

        await api.post(url(project, "requirements/R1/answers"), json={"answer_markdown": "Changed."}, headers=A)

        assert (await api.get(url(project, "requirements/R1"), headers=A)).json()["status"] == "edited"

    async def test_the_answer_event_names_the_version_not_the_text(self, api):
        project = await make_project(api)
        await api.post(url(project, "requirements/R1/answers"), json={"answer_markdown": "Secret plan."}, headers=A)
        (event, *_) = (await api.get(url(project, "events"), params={"rid": "R1"}, headers=A)).json()["events"]
        assert event["action"] == "answer.added" and event["details"] == {"version": 1, "source": "user"}

    async def test_what_a_person_writes_is_redacted_like_everything_else(self, api):
        project = await make_project(api)
        body = {"answer_markdown": "Mail jane@example.com", "risks": ["costs $90,000"], "evidence": [{"title": "t", "quote": "call 555-123-4567"}]}
        saved = (await api.post(url(project, "requirements/R1/answers"), json=body, headers=A)).json()["answer"]
        assert "jane@example.com" not in saved["answer_markdown"] and "90,000" not in saved["risks"][0]
        assert "555-123-4567" not in saved["evidence"][0]["quote"]

    async def test_an_answer_is_written_or_restored_not_both_and_not_neither(self, api):
        project = await make_project(api)
        both = {"answer_markdown": "x", "restore_version": 1}
        for body in (both, {}, {"meets": "Maybe", "answer_markdown": "x"}):
            assert (await api.post(url(project, "requirements/R1/answers"), json=body, headers=A)).status_code == 422

    async def test_two_people_adding_a_version_at_once_do_not_both_win(self, api):
        project = await make_project(api, share_with_groups=["team-1"])
        await api.post(url(project, "requirements/R1/answers"), json={"answer_markdown": "One."}, headers=A)
        stale = await api.post(url(project, "requirements/R1/answers"), json={"answer_markdown": "Mine.", "expected_rev": 1}, headers=B)
        assert stale.status_code == 409
        versions = (await api.get(url(project, "requirements/R1/answers"), headers=A)).json()["answers"]
        assert [v["answer_markdown"] for v in versions] == ["One."]


# ── The service on its own (model answers, for the draft jobs) ──


class TestModelAnswers:
    async def test_a_model_answer_is_recorded_as_drafted_with_its_job(self, api):
        from app.ledger import service

        project = await make_project(api)
        store = ledger_store.store_for(api_settings())
        model_answer = {"requirement_id": "R1", "answer_markdown": "AES-256 [Guide](/kb/doc/abc).", "meets": "Yes",
                        "evidence": [{"title": "Guide", "source_url": "/kb/doc/abc", "quote": "AES-256"}],
                        "kb_evidence_found": True, "risks": ["none"]}

        requirement = service.add_answer(store, api_settings(), project["project"]["id"], "R1", model_answer,
                                         actor=service.AGENT, source="model", job_id="job-1")

        assert requirement["status"] == "drafted" and requirement["answer"]["source"] == "model"
        assert requirement["answer"]["author"] == "agent" and requirement["answer"]["job_id"] == "job-1"
        assert requirement["answer"]["kb_evidence_found"] is True
        (event, *_) = (await api.get(url(project, "events"), params={"rid": "R1"}, headers=A)).json()["events"]
        assert event["actor"] == "agent" and event["details"] == {"version": 1, "source": "model", "job_id": "job-1"}


def api_settings():
    from .test_authorization_api import make_settings

    return make_settings("easyauth", ledger_backend="memory")


class TestClock:
    def test_events_written_back_to_back_still_sort_in_the_order_they_happened(self):
        from app.ledger import service

        stamps = [service._now() for _ in range(500)]
        assert stamps == sorted(stamps) and len(set(stamps)) == 500
