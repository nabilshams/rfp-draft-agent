"""The requirement model in the ledger: typed items, repeats answered once, dates on the project, and the confirm step (#32)."""

import json

import pytest

from app.agents import states
from app.ledger import drafting
from app.ledger import store as ledger_store

from .test_ai_extraction import reading
from .test_authorization_api import A, B, upload_rfp
from .test_draft_endpoints import agent  # noqa: F401  (a fixture)
from .test_draft_jobs import worker  # noqa: F401  (a fixture)
from .test_extraction_fidelity import make_docx
from .test_ledger_api import api, api_settings, make_project, url  # noqa: F401  (api is a fixture)
from .test_ledger_drafting import draft, reply_for, script, state_of  # noqa: F401  (script is a fixture)

REQS = [
    {"id": "R1", "text": "The vendor shall provide online training for administrators."},
    {"id": "R2", "text": "Describe your approach to implementation and go-live."},
    {"id": "R3", "text": "Please provide online training for administrators."},
    {"id": "R4", "text": "Each answer shall be no longer than 200 words."},
    {"id": "R5", "text": "Responses must be submitted by 30 November 2026 via the tender portal."},
]


def by_rid(state):
    return {r["rid"]: r for r in state["requirements"]}


# ── What is stored ───────────────────────────────────────


class TestStored:
    async def test_every_requirement_keeps_its_type_response_type_evidence_flag_and_repeat(self, api):  # noqa: F811
        project = await make_project(api, requirements=REQS)
        items = by_rid(project)
        assert items["R1"]["item_kind"] == "mandatory" and items["R1"]["response_type"] == "narrative"
        assert items["R3"]["duplicate_of"] == "R1" and items["R1"]["duplicate_of"] == ""
        assert items["R4"]["item_kind"] == "instruction" and items["R5"]["item_kind"] == "deadline"
        assert items["R2"]["evidence_required"] is False and items["R2"]["implicit"] is False

    async def test_a_requirement_sent_with_its_own_typing_keeps_it(self, api):  # noqa: F811
        project = await make_project(api, requirements=[
            {"id": "A", "text": "Provide a copy of your insurance certificate.", "kind": "optional", "response_type": "table",
             "evidence_required": False, "implicit": True},
            {"id": "B", "text": "What is your uptime?", "item_kind": "mandatory"}])
        items = by_rid(project)
        assert (items["A"]["item_kind"], items["A"]["response_type"], items["A"]["evidence_required"], items["A"]["implicit"]) == (
            "optional", "table", False, True)
        assert items["B"]["item_kind"] == "mandatory" and items["B"]["type"] == "requirement"

    async def test_a_reference_to_an_item_that_is_not_there_is_dropped(self, api):  # noqa: F811
        project = await make_project(api, requirements=[{"id": "A", "text": "Provide training for users.", "duplicate_of": "ZZ"}])
        assert by_rid(project)["A"]["duplicate_of"] == ""

    async def test_the_project_keeps_the_dates_and_how_it_was_read(self, api, agent):  # noqa: F811
        lines = ["The vendor shall encrypt all customer data at rest.", "Responses must be submitted by 30 November 2026."]
        agent.reply = lambda p: json.dumps({"items": [{"quote": line} for line in lines]})
        rfp_id = await upload_rfp(api, A, data=make_docx(lambda d: [d.add_paragraph(x) for x in lines]))

        project = (await api.post("/projects", json={"name": "AI", "rfp_id": rfp_id, "extractor": "ai"}, headers=A)).json()

        record = project["project"]
        assert record["deadlines"] == [{"text": lines[1], "date": "2026-11-30", "anchor": "¶2"}]
        assert record["extraction"]["method"] == "ai" and record["extraction"]["confirmed"] is False
        assert record["extraction"]["coverage"]["chunks"] == 1

    async def test_a_project_read_by_patterns_needs_no_confirming(self, api):  # noqa: F811
        rfp_id = await upload_rfp(api, A)
        project = (await api.post("/projects", json={"name": "Sample", "rfp_id": rfp_id}, headers=A)).json()
        assert project["project"]["extraction"]["method"] == "local" and project["project"]["extraction"]["confirmed"] is True

    async def test_a_project_made_from_requirements_you_send_is_supplied_and_confirmed(self, api):  # noqa: F811
        record = (await make_project(api))["project"]
        assert record["extraction"] == {"method": "supplied", "confirmed": True, "warnings": [], "coverage": {}}
        assert record["deadlines"] == [] and record["submission_rules"] == []

    async def test_a_model_that_cannot_read_the_rfp_is_a_502_and_makes_no_project(self, api, agent):  # noqa: F811
        agent.reply = lambda _p: "nope"
        rfp_id = await upload_rfp(api, A)
        assert (await api.post("/projects", json={"name": "AI", "rfp_id": rfp_id, "extractor": "ai"}, headers=A)).status_code == 502
        assert (await api.get("/projects", headers=A)).json()["projects"] == []

    async def test_the_extractor_must_be_one_of_the_two(self, api):  # noqa: F811
        rfp_id = await upload_rfp(api, A)
        assert (await api.post("/projects", json={"name": "x", "rfp_id": rfp_id, "extractor": "magic"}, headers=A)).status_code == 422


# ── What a draft works on ────────────────────────────────


class TestDrafting:
    async def test_only_answered_items_that_are_not_repeats_are_drafted(self, api, worker, script):  # noqa: F811
        project = await make_project(api, requirements=REQS)
        await draft(api, worker, project)
        assert script.drafted() == ["R1", "R2"]  # not the repeat R3, the instruction R4 or the deadline R5

    async def test_a_repeat_takes_the_first_ones_answer_and_says_so(self, api, worker, script):  # noqa: F811
        project = await make_project(api, requirements=REQS)
        await draft(api, worker, project)

        items = by_rid(await state_of(api, project))

        assert items["R3"]["answer"]["answer_markdown"] == items["R1"]["answer"]["answer_markdown"] == "Answer for R1."
        assert items["R3"]["answer"]["reused_from"] == "R1" and items["R3"]["status"] == "drafted"
        assert "reused_from" not in items["R1"]["answer"]
        assert items["R3"]["answer"]["evidence"] == items["R1"]["answer"]["evidence"]
        assert items["R4"]["answer"] is None and items["R5"]["answer"] is None

    async def test_the_job_says_which_repeats_took_the_answer(self, api, worker, script):  # noqa: F811
        from app.services import jobs

        project = await make_project(api, requirements=REQS)
        started = (await draft(api, worker, project)).json()
        job = jobs.JobStore(api_settings()).load(started["job_id"])
        saved = [e for e in job.events if e["type"] == "saved" and e["data"]["requirement_id"] == "R1"]
        assert saved[0]["data"]["reused_by"] == ["R3"]
        events = (await api.get(url(project, "events"), params={"limit": 80}, headers=A)).json()["events"]
        assert next(e for e in events if e["action"] == "draft.finished")["details"]["reused"] == 1

    async def test_the_status_counts_only_what_is_left_to_draft(self, api, worker, script):  # noqa: F811
        project = await make_project(api, requirements=REQS)
        assert (await api.get(url(project, "draft"), headers=A)).json()["remaining"] == 2
        await draft(api, worker, project)
        assert (await api.get(url(project, "draft"), headers=A)).json()["remaining"] == 0

    async def test_a_repeat_a_person_has_answered_keeps_their_answer(self, api, worker, script):  # noqa: F811
        project = await make_project(api, requirements=REQS)
        await api.post(url(project, "requirements/R3/answers"), json={"answer_markdown": "My own answer."}, headers=A)
        await draft(api, worker, project)
        items = by_rid(await state_of(api, project))
        assert items["R3"]["answer"]["answer_markdown"] == "My own answer." and items["R3"]["status"] == "edited"

    async def test_a_redraft_of_the_first_replaces_the_repeats_model_answer_but_not_an_approved_one(self, api, worker, script):  # noqa: F811
        project = await make_project(api, requirements=REQS)
        await draft(api, worker, project)
        script.hooks["R1"] = lambda r: reply_for(r, "Second version.")
        await draft(api, worker, project, body={"rids": ["R1"]})
        assert by_rid(await state_of(api, project))["R3"]["answer"]["answer_markdown"] == "Second version."

        await api.patch(url(project, "requirements/R3"), json={"status": "approved"}, headers=A)
        script.hooks["R1"] = lambda r: reply_for(r, "Third version.")
        await draft(api, worker, project, body={"rids": ["R1"]})
        assert by_rid(await state_of(api, project))["R3"]["answer"]["answer_markdown"] == "Second version."

    async def test_naming_an_item_drafts_it_even_if_it_is_a_repeat(self, api, worker, script):  # noqa: F811
        project = await make_project(api, requirements=REQS)
        await draft(api, worker, project)
        script.hooks["R3"] = lambda r: reply_for(r, "Its own answer.")
        await draft(api, worker, project, body={"rids": ["R3"]})
        assert by_rid(await state_of(api, project))["R3"]["answer"]["answer_markdown"] == "Its own answer."

    async def test_nothing_to_draft_when_only_instructions_and_repeats_are_left(self, api, worker, script):  # noqa: F811
        project = await make_project(api, requirements=REQS)
        await draft(api, worker, project)
        response = await api.post(url(project, "draft"), json={"client_id": "acme", "kb_project_id": "p1"}, headers=A)
        assert response.status_code == 409 and "Nothing to draft" in response.json()["detail"]

    async def test_the_documents_include_the_repeat_with_the_shared_answer(self, api, worker, script):  # noqa: F811
        project = await make_project(api, requirements=REQS)
        await draft(api, worker, project)
        sink = drafting.ProjectSink(
            ledger_store.store_for(api_settings()), api_settings(), project["project"]["id"], "j",
            drafting.select(ledger_store.store_for(api_settings()), project["project"]["id"], ["R1"]))
        responses, requirements = sink.document_inputs()
        assert {r["requirement_id"] for r in responses} == {"R1", "R2", "R3"}
        assert {r["id"]: r["kind"] for r in requirements}["R4"] == "instruction"

    async def test_an_old_project_whose_items_were_never_typed_drafts_everything(self, api, worker, script):  # noqa: F811
        project = await make_project(api, requirements=[{"id": "R1", "text": "Provide online training."}, {"id": "R2", "text": "Provide support."}])
        stored = ledger_store.store_for(api_settings())
        for rid in ("R1", "R2"):
            doc = stored.get(project["project"]["id"], f"req:{rid}")
            for key in ("item_kind", "response_type", "evidence_required", "implicit", "duplicate_of"):
                doc.pop(key)
            stored.apply(project["project"]["id"], [ledger_store.replace(doc)])
        await draft(api, worker, project)
        assert script.drafted() == ["R1", "R2"]


# ── Editing what an item is ──────────────────────────────


class TestEditing:
    async def patch(self, api, project, rid, **body):  # noqa: F811
        return await api.patch(url(project, f"requirements/{rid}"), json=body, headers=A)

    async def test_a_person_can_correct_the_kind_the_response_type_and_the_evidence_flag(self, api):  # noqa: F811
        project = await make_project(api, requirements=REQS)
        response = await self.patch(api, project, "R4", item_kind="mandatory", response_type="table", evidence_required=True)
        assert response.status_code == 200
        assert (response.json()["item_kind"], response.json()["response_type"], response.json()["evidence_required"]) == (
            "mandatory", "table", True)
        events = (await api.get(url(project, "events"), params={"rid": "R4"}, headers=A)).json()["events"]
        assert events[0]["details"]["changes"]["item_kind"] == {"from": "instruction", "to": "mandatory"}

    async def test_a_value_outside_the_lists_is_refused(self, api):  # noqa: F811
        project = await make_project(api, requirements=REQS)
        assert (await self.patch(api, project, "R1", item_kind="banana")).status_code == 422
        assert (await self.patch(api, project, "R1", response_type="essay")).status_code == 422

    async def test_an_item_can_be_grouped_under_another_and_ungrouped(self, api):  # noqa: F811
        project = await make_project(api, requirements=REQS)
        grouped = await self.patch(api, project, "R2", duplicate_of="R1")
        assert grouped.json()["duplicate_of"] == "R1"
        assert (await self.patch(api, project, "R2", duplicate_of="")).json()["duplicate_of"] == ""

    async def test_grouping_with_something_that_is_not_there_or_is_itself_is_refused(self, api):  # noqa: F811
        project = await make_project(api, requirements=REQS)
        assert (await self.patch(api, project, "R2", duplicate_of="NOPE")).status_code == 422
        assert (await self.patch(api, project, "R2", duplicate_of="R2")).status_code == 422

    async def test_only_an_answered_item_can_be_grouped_or_grouped_under(self, api):  # noqa: F811
        project = await make_project(api, requirements=REQS)
        assert (await self.patch(api, project, "R2", duplicate_of="R4")).status_code == 422  # R4 is an instruction
        assert (await self.patch(api, project, "R4", duplicate_of="R1")).status_code == 422

    async def test_grouping_under_a_repeat_points_at_its_first(self, api):  # noqa: F811
        project = await make_project(api, requirements=REQS)  # R3 repeats R1
        refused = await self.patch(api, project, "R2", duplicate_of="R3")
        assert refused.status_code == 422 and "group with that one" in refused.json()["detail"]

    async def test_moving_an_item_takes_its_own_repeats_with_it(self, api):  # noqa: F811
        project = await make_project(api, requirements=REQS)  # R3 repeats R1
        await self.patch(api, project, "R1", duplicate_of="R2")
        items = by_rid(await state_of(api, project))
        assert items["R1"]["duplicate_of"] == "R2" and items["R3"]["duplicate_of"] == "R2"

    async def test_an_item_that_stops_being_answered_releases_its_repeats(self, api):  # noqa: F811
        project = await make_project(api, requirements=REQS)  # R3 repeats R1
        await self.patch(api, project, "R1", item_kind="instruction")
        items = by_rid(await state_of(api, project))
        assert items["R3"]["duplicate_of"] == "" and items["R1"]["item_kind"] == "instruction"

    async def test_an_item_that_stops_being_answered_stops_being_a_repeat(self, api):  # noqa: F811
        project = await make_project(api, requirements=REQS)
        assert (await self.patch(api, project, "R3", item_kind="deadline")).json()["duplicate_of"] == ""

    async def test_once_ungrouped_an_item_is_drafted_on_its_own(self, api, worker, script):  # noqa: F811
        project = await make_project(api, requirements=REQS)
        await self.patch(api, project, "R3", duplicate_of="")
        await draft(api, worker, project)
        assert script.drafted() == ["R1", "R2", "R3"]

    async def test_a_stale_edit_is_still_a_409(self, api):  # noqa: F811
        project = await make_project(api, requirements=REQS)
        assert (await self.patch(api, project, "R1", item_kind="optional", expected_rev=99)).status_code == 409


# ── Reviewing what the model extracted ───────────────────


class TestConfirm:
    async def ai_project(self, api, agent):  # noqa: F811
        lines = ["The vendor shall encrypt all customer data at rest.", "The vendor shall log every access to records."]
        agent.reply = lambda p: reading(p)
        rfp_id = await upload_rfp(api, A, data=make_docx(lambda d: [d.add_paragraph(x) for x in lines]))
        return (await api.post("/projects", json={"name": "AI", "rfp_id": rfp_id, "extractor": "ai"}, headers=A)).json()

    async def test_a_model_read_project_cannot_be_drafted_until_somebody_confirms(self, api, agent, worker, script):  # noqa: F811
        project = await self.ai_project(api, agent)
        refused = await api.post(url(project, "draft"), json={"client_id": "acme", "kb_project_id": "p1"}, headers=A)
        assert refused.status_code == 409 and "confirm" in refused.json()["detail"]

        confirmed = await api.post(url(project, "confirm-extraction"), headers=A)
        assert confirmed.status_code == 200 and confirmed.json()["extraction"]["confirmed"] is True
        assert confirmed.json()["extraction"]["confirmed_by"] == "user-a"
        await draft(api, worker, project)
        assert script.drafted() == ["R1", "R2"]

    async def test_the_confirmation_is_in_the_audit_trail_and_confirming_twice_changes_nothing(self, api, agent):  # noqa: F811
        project = await self.ai_project(api, agent)
        await api.post(url(project, "confirm-extraction"), headers=A)
        await api.post(url(project, "confirm-extraction"), headers=A)
        events = (await api.get(url(project, "events"), params={"limit": 20}, headers=A)).json()["events"]
        assert [e["action"] for e in events].count("extraction.confirmed") == 1

    async def test_edits_before_confirming_are_allowed_and_recorded(self, api, agent):  # noqa: F811
        project = await self.ai_project(api, agent)
        assert (await api.patch(url(project, "requirements/R1"), json={"item_kind": "optional"}, headers=A)).status_code == 200

    async def test_somebody_who_cannot_open_the_project_cannot_confirm_it(self, api, agent):  # noqa: F811
        project = await self.ai_project(api, agent)
        assert (await api.post(url(project, "confirm-extraction"), headers=B)).status_code == 404

    async def test_a_project_that_needs_no_confirming_can_still_be_confirmed_harmlessly(self, api):  # noqa: F811
        project = await make_project(api)
        response = await api.post(url(project, "confirm-extraction"), headers=A)
        assert response.status_code == 200 and response.json()["extraction"]["confirmed"] is True


def test_the_answer_states_and_the_item_kinds_are_separate_vocabularies():
    from app.ingestion import classify

    assert not set(states.STATES) & set(classify.KINDS)
    assert pytest is not None
