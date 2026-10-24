"""Project drafts: answers are saved to the ledger as they are produced, and a draft picks up where it left off."""

import pytest
from azure.core.exceptions import HttpResponseError

from app.agents import answers, strategies
from app.ledger import drafting, service
from app.ledger import store as ledger_store
from app.services import jobs

from .test_authorization_api import ADMIN, KB_BLOB, A, B, C, as_user
from .test_draft_jobs import worker  # noqa: F401  (a fixture: work is run when the test says so)
from .test_ledger_api import REQUIREMENTS, api, api_settings, make_project, url  # noqa: F401  (api is a fixture)

BODY = {"client_id": "acme", "kb_project_id": "p1"}


def reply_for(requirement, text=None):
    return {
        "requirement_id": requirement["id"],
        "requirement_text": requirement["text"],
        "answer_markdown": text or f"Answer for {requirement['id']}.",
        "meets": "Yes",
        "evidence": [
            {"title": "Policy", "source_url": "/kb/doc/abc", "quote": "AES-256", "source_type": "authoritative",
             "effective_date": "2026-05-01", "version": "4", "section": "3. Encryption", "page": 4}
        ],
        "assumptions": ["a"],
        "risks": ["r"],
        "followups": [],
        "kb_evidence_found": True,
    }


class Script:
    """Stands in for the drafting strategy: records what it was asked to draft; `hooks` change what happens per requirement."""

    def __init__(self):
        self.received = []
        self.guidance = []
        self.hooks = {}

    def drafted(self):
        return [rid for batch in self.received for rid in batch]


@pytest.fixture
def script(monkeypatch):
    state = Script()

    def fake(**kwargs):
        state.received.append([r["id"] for r in kwargs["requirements"]])
        state.guidance.append(kwargs["guidance"])
        for requirement in kwargs["requirements"]:
            hook = state.hooks.get(requirement["id"])
            result = (hook(requirement) if hook else None) or reply_for(requirement)
            yield result

    monkeypatch.setattr(strategies, "stream_per_requirement", fake)
    return state


def store():
    return ledger_store.store_for(api_settings())


async def draft(api, worker, project, who=A, body=None, expect=202):  # noqa: F811
    response = await api.post(url(project, "draft"), json={**BODY, **(body or {})}, headers=who)
    assert response.status_code == expect, response.text
    if expect == 202:
        worker.run_all()
    return response


async def state_of(api, project, who=A):  # noqa: F811
    return (await api.get(url(project), headers=who)).json()


def answered(state):
    return {r["rid"]: r["answer"]["answer_markdown"] if r["answer"] else None for r in state["requirements"]}


# ── Saving as it goes ────────────────────────────────────


class TestSavingAnswers:
    async def test_each_answer_is_saved_to_the_ledger_with_its_evidence_and_job(self, api, worker, script):  # noqa: F811
        project = await make_project(api)

        started = (await draft(api, worker, project)).json()

        state = await state_of(api, project)
        assert answered(state) == {"R1": "Answer for R1.", "R2": "Answer for R2.", "R3": "Answer for R3."}
        first = state["requirements"][0]
        assert first["status"] == "drafted" and first["answer_version"] == 1 and first["rev"] == 2
        answer = first["answer"]
        assert (answer["source"], answer["author"], answer["job_id"]) == ("model", "agent", started["job_id"])
        assert answer["meets"] == "Yes" and answer["kb_evidence_found"] is True and answer["risks"] == ["r"]
        # where the evidence came from is kept, not just the link
        assert answer["evidence"][0] == {
            "title": "Policy", "source_url": "/kb/doc/abc", "quote": "AES-256", "source_type": "authoritative",
            "effective_date": "2026-05-01", "version": "4", "section": "3. Encryption", "page": 4,
        }
        assert state["counts"] == {"extracted": 0, "drafted": 3, "edited": 0, "approved": 0, "needs_input": 0}

    async def test_the_draft_is_recorded_in_the_audit_trail(self, api, worker, script):  # noqa: F811
        project = await make_project(api)
        started = (await draft(api, worker, project)).json()

        events = (await api.get(url(project, "events"), params={"limit": 50}, headers=A)).json()["events"]
        actions = [e["action"] for e in events]
        assert actions.count("answer.added") == 3 and "draft.started" in actions and "draft.finished" in actions
        began = next(e for e in events if e["action"] == "draft.started")
        assert began["actor"] == "user-a" and began["details"]["job_id"] == started["job_id"]
        finished = next(e for e in events if e["action"] == "draft.finished")
        assert finished["details"] == {"job_id": started["job_id"], "outcome": "ok", "saved": 3, "changed_meanwhile": 0, "failed": 0, "reused": 0}

    async def test_the_job_reports_the_saved_answers(self, api, worker, script):  # noqa: F811
        project = await make_project(api)
        started = (await draft(api, worker, project)).json()
        job = jobs.JobStore(api_settings()).load(started["job_id"])
        kinds = [e["type"] for e in job.events]
        assert kinds == ["start", "response", "saved", "response", "saved", "response", "saved", "complete"]
        assert job.status == "succeeded" and job.project_id == project["project"]["id"]

    async def test_the_rfps_guidance_reaches_the_drafting(self, api, worker, script):  # noqa: F811
        project = await make_project(api)
        stored = store().get(project["project"]["id"], "project")
        store().apply(project["project"]["id"], [ledger_store.replace({**stored, "guidance": "Answers under 200 words."})])
        await draft(api, worker, project)
        assert script.guidance == ["Answers under 200 words."]

    async def test_the_documents_are_built_from_the_whole_project_and_published_under_it(self, api, worker, script, world):  # noqa: F811
        project = await make_project(api, share_with_groups=["team-1"])
        await api.post(url(project, "requirements/R1/answers"), json={"answer_markdown": "Hand-written answer to R1."}, headers=A)
        await draft(api, worker, project)  # drafts only R2 and R3

        assert script.drafted() == ["R2", "R3"]
        project_id = project["project"]["id"]
        assert {b.name for b in world.storage.outputs.list_blobs()} == {f"{project_id}/draft.docx", f"{project_id}/compliance.xlsx"}
        import io

        import docx

        text = "\n".join(p.text for p in docx.Document(io.BytesIO(world.storage.outputs.blobs[f"{project_id}/draft.docx"].data)).paragraphs)
        assert "Hand-written answer to R1." in text and "Answer for R2." in text  # R1 was not drafted by this run

    async def test_members_download_the_documents_through_the_project(self, api, worker, script, world):  # noqa: F811
        project = await make_project(api, share_with_groups=["team-1"])
        await draft(api, worker, project)
        project_id = project["project"]["id"]

        assert (await api.get(f"/outputs/{project_id}/draft.docx", headers=A)).status_code == 200
        assert (await api.get(f"/outputs/{project_id}/compliance.xlsx", headers=B)).status_code == 200  # same group
        assert (await api.get(f"/outputs/{project_id}/draft.docx", headers=C)).status_code == 404
        status = (await api.get(url(project, "draft"), headers=B)).json()
        assert status["job"]["downloads"] == {"docx": f"/outputs/{project_id}/draft.docx", "xlsx": f"/outputs/{project_id}/compliance.xlsx"}

    async def test_section_approaches_save_the_compliance_answers_too(self, api, worker, monkeypatch):  # noqa: F811
        def fake(**kwargs):
            for requirement in kwargs["requirements"]:
                reply = reply_for(requirement)
                yield {"type": "requirement_compliance", "data": {k: reply[k] for k in ("requirement_id", "meets", "answer_markdown", "evidence")}}

        monkeypatch.setattr(strategies, "stream_per_section", fake)
        project = await make_project(api)
        await draft(api, worker, project, body={"approach": "per_section"})
        state = await state_of(api, project)
        assert [r["answer_version"] for r in state["requirements"]] == [1, 1, 1]


# ── Picking up where it left off ─────────────────────────


class TestResuming:
    async def test_a_failed_draft_keeps_what_it_finished_and_the_next_run_does_only_the_rest(self, api, worker, script):  # noqa: F811
        project = await make_project(api)

        def crash(_requirement):
            raise RuntimeError("the model service went away")

        script.hooks["R2"] = crash
        await draft(api, worker, project)
        assert (await state_of(api, project))["counts"]["drafted"] == 1
        status = (await api.get(url(project, "draft"), headers=A)).json()
        assert status["job"]["status"] == "failed" and status["remaining"] == 2 and status["running"] is False

        script.hooks.clear()
        await draft(api, worker, project)

        assert script.received == [["R1", "R2", "R3"], ["R2", "R3"]]  # R1 was not generated again
        assert set(answered(await state_of(api, project)).values()) == {"Answer for R1.", "Answer for R2.", "Answer for R3."}

    async def test_when_everything_is_answered_there_is_nothing_to_draft(self, api, worker, script):  # noqa: F811
        project = await make_project(api)
        await draft(api, worker, project)
        refused = await draft(api, worker, project, expect=409)
        assert "Nothing to draft" in refused.json()["detail"]
        assert script.received == [["R1", "R2", "R3"]]

    async def test_a_worker_that_died_is_noticed_and_the_draft_can_be_started_again(self, api, worker, script):  # noqa: F811
        project = await make_project(api)
        # a draft whose job never finishes: the worker "dies" with the job unfinished and silent
        response = await api.post(url(project, "draft"), json=BODY, headers=A)
        job_id = response.json()["job_id"]
        jobs.reset()  # the process that was running it is gone
        job_store = jobs.JobStore(api_settings())
        job = job_store.load(job_id)
        job.heartbeat_at = "2020-01-01T00:00:00+00:00"
        job_store.save(job)

        status = (await api.get(url(project, "draft"), headers=A)).json()
        assert status["interrupted"] is True and status["running"] is False
        assert status["job"]["error"].startswith("The draft was interrupted")
        assert status["remaining"] == 3

        again = await api.post(url(project, "draft"), json=BODY, headers=A)
        assert again.status_code == 202 and again.json()["job_id"] != job_id

    async def test_an_answer_that_failed_is_not_saved_and_is_tried_again_next_time(self, api, worker, script):  # noqa: F811
        project = await make_project(api)
        script.hooks["R2"] = lambda r: answers.error_answer(r, message="Error: boom", risk="boom")

        await draft(api, worker, project)

        state = await state_of(api, project)
        assert answered(state)["R2"] is None and state["counts"]["drafted"] == 2  # nothing "meets: No" was invented
        job = jobs.JobStore(api_settings()).load((await api.get(url(project, "draft"), headers=A)).json()["job"]["job_id"])
        assert {"type": "not_saved", "data": {"requirement_id": "R2", "reason": "failed"}} in job.events
        finished = next(e for e in (await api.get(url(project, "events"), headers=A)).json()["events"] if e["action"] == "draft.finished")
        assert finished["details"]["failed"] == 1

        script.hooks.clear()
        await draft(api, worker, project)
        assert script.received[-1] == ["R2"]

    async def test_a_cancelled_draft_keeps_every_answer_it_had_paid_for(self, api, worker, script):  # noqa: F811
        project = await make_project(api)
        holder = {}

        def cancel_while_working_on_r2(_requirement):
            jobs.JobStore(api_settings()).request_cancel(holder["job"])

        script.hooks["R2"] = cancel_while_working_on_r2
        response = await api.post(url(project, "draft"), json=BODY, headers=A)
        holder["job"] = response.json()["job_id"]
        worker.run_all()

        state = await state_of(api, project)
        assert answered(state) == {"R1": "Answer for R1.", "R2": "Answer for R2.", "R3": None}
        status = (await api.get(url(project, "draft"), headers=A)).json()
        assert status["job"]["status"] == "cancelled" and status["remaining"] == 1
        finished = next(e for e in (await api.get(url(project, "events"), headers=A)).json()["events"] if e["action"] == "draft.finished")
        assert finished["details"]["outcome"] == "cancelled" and finished["details"]["saved"] == 2


# ── People and the model working at the same time ────────


class TestConcurrentWork:
    async def test_only_one_draft_runs_per_project(self, api, worker, script):  # noqa: F811
        project = await make_project(api, share_with_groups=["team-1"])
        first = await api.post(url(project, "draft"), json=BODY, headers=A)  # queued, not yet run
        assert first.status_code == 202

        for who in (A, B):
            clash = await api.post(url(project, "draft"), json=BODY, headers=who)
            assert clash.status_code == 409 and first.json()["job_id"] in clash.json()["detail"]

        worker.run_all()
        assert script.received == [["R1", "R2", "R3"]]  # one job ran, once

    async def test_a_claim_whose_job_is_not_recorded_yet_still_counts_as_running(self, api, worker, script):  # noqa: F811
        """Regression: in the moment between claiming the project and the job record being written, a second request
        used to see "no job", claim over the first and start a second draft of the same requirements."""
        project = await make_project(api, share_with_groups=["team-1"])
        project_id = project["project"]["id"]
        current = store().get(project_id, "project")
        store().apply(project_id, [ledger_store.replace({**current, "draft_job": {"id": "f" * 32, "by": "user-a", "at": service._now()}})])

        clash = await api.post(url(project, "draft"), json=BODY, headers=B)
        assert clash.status_code == 409 and "just starting" in clash.json()["detail"]
        status = (await api.get(url(project, "draft"), headers=B)).json()
        assert status["running"] is True and status["job"] is None
        assert (await api.post(url(project, "draft/cancel"), headers=A)).status_code == 409  # nothing to stop yet
        assert worker.tasks == []

    async def test_an_old_claim_with_no_job_is_a_crash_not_a_running_draft(self, api, worker, script):  # noqa: F811
        project = await make_project(api)
        project_id = project["project"]["id"]
        current = store().get(project_id, "project")
        store().apply(project_id, [ledger_store.replace({**current, "draft_job": {"id": "f" * 32, "by": "user-a", "at": "2020-01-01T00:00:00.000000Z"}})])

        assert (await api.get(url(project, "draft"), headers=A)).json()["running"] is False
        assert (await api.post(url(project, "draft"), json=BODY, headers=A)).status_code == 202

    async def test_two_people_pressing_the_button_together_start_one_draft(self, api, worker, script, monkeypatch):  # noqa: F811
        project = await make_project(api, share_with_groups=["team-1"])
        real = store().apply
        calls = []

        def somebody_claims_first(project_id, ops):
            calls.append(1)
            if len(calls) == 1:  # the other person's claim lands between our read and our write
                current = store().get(project_id, "project")
                real(project_id, [ledger_store.replace({**current, "draft_job": {"id": "other", "by": "user-b", "at": "now"}})])
            return real(project_id, ops)

        monkeypatch.setattr(store(), "apply", somebody_claims_first)
        response = await api.post(url(project, "draft"), json=BODY, headers=A)
        assert response.status_code == 409 and "just started" in response.json()["detail"]
        assert worker.tasks == []

    async def test_a_person_editing_while_the_draft_runs_is_not_overwritten(self, api, worker, script):  # noqa: F811
        project = await make_project(api)

        def person_edits_r1(_requirement):
            service.add_answer(
                store(), api_settings(), project["project"]["id"], "R1", {"answer_markdown": "My own answer."},
                actor=service.Actor("user-a", "Al"), source="user",
            )

        script.hooks["R1"] = person_edits_r1
        await draft(api, worker, project)

        state = await state_of(api, project)
        assert answered(state)["R1"] == "My own answer." and state["requirements"][0]["status"] == "edited"
        assert answered(state)["R2"] == "Answer for R2."  # the rest carried on
        job = jobs.JobStore(api_settings()).load((await api.get(url(project, "draft"), headers=A)).json()["job"]["job_id"])
        assert {"type": "not_saved", "data": {"requirement_id": "R1", "reason": "changed"}} in job.events
        assert job.status == "succeeded"
        finished = next(e for e in (await api.get(url(project, "events"), headers=A)).json()["events"] if e["action"] == "draft.finished")
        assert finished["details"]["changed_meanwhile"] == 1

    async def test_human_and_approved_answers_are_left_alone_unless_named(self, api, worker, script):  # noqa: F811
        project = await make_project(api)
        await api.post(url(project, "requirements/R1/answers"), json={"answer_markdown": "Mine."}, headers=A)
        await api.post(url(project, "requirements/R2/answers"), json={"answer_markdown": "Approved text."}, headers=A)
        await api.patch(url(project, "requirements/R2"), json={"status": "approved"}, headers=A)

        await draft(api, worker, project)
        assert script.received == [["R3"]]

        await draft(api, worker, project, body={"rids": ["R2"]})  # naming it is asking for it to be redone
        state = await state_of(api, project)
        assert script.received[-1] == ["R2"]
        assert state["requirements"][1]["answer"]["answer_markdown"] == "Answer for R2." and state["requirements"][1]["answer_version"] == 2
        assert state["requirements"][1]["status"] == "drafted"  # the approval is withdrawn
        versions = (await api.get(url(project, "requirements/R2/answers"), headers=A)).json()["answers"]
        assert [v["answer_markdown"] for v in versions] == ["Answer for R2.", "Approved text."]  # nothing was lost
        assert answered(state)["R1"] == "Mine."

    async def test_a_ledger_hiccup_is_retried_and_a_lasting_one_fails_the_job_but_keeps_earlier_answers(self, api, worker, script, monkeypatch):  # noqa: F811
        monkeypatch.setattr(drafting.time, "sleep", lambda _s: None)
        project = await make_project(api)
        real = store().apply
        outcome = {"failures": 0, "mode": "flaky"}

        def hiccup(project_id, ops):
            is_answer = any(o.doc.get("kind") == "answer" for o in ops)
            if is_answer and outcome["mode"] == "flaky" and outcome["failures"] < 2:
                outcome["failures"] += 1
                raise HttpResponseError(message="429 throttled")
            if is_answer and outcome["mode"] == "down" and any(o.doc.get("rid") == "R3" for o in ops):
                raise HttpResponseError(message="503 unavailable")
            return real(project_id, ops)

        monkeypatch.setattr(store(), "apply", hiccup)
        await draft(api, worker, project)
        state = await state_of(api, project)
        assert state["counts"]["drafted"] == 3 and outcome["failures"] == 2  # retried, nothing lost

        # now a lasting failure while saving R3 (after R1 and R2 were saved)
        await api.post(url(project, "draft"), json={**BODY, "rids": ["R1", "R2", "R3"]}, headers=A)
        outcome["mode"] = "down"
        worker.run_all()
        job_id = (await api.get(url(project, "draft"), headers=A)).json()["job"]["job_id"]
        assert jobs.JobStore(api_settings()).load(job_id).status == "failed"
        state = await state_of(api, project)
        assert [r["answer_version"] for r in state["requirements"]] == [2, 2, 1]  # R1 and R2 were redone and kept


# ── Access and limits ────────────────────────────────────


class TestAccess:
    async def test_a_project_you_cannot_open_cannot_be_drafted_watched_or_stopped(self, api, worker, script):  # noqa: F811
        project = await make_project(api)
        assert (await api.post(url(project, "draft"), json=BODY, headers=B)).status_code == 404
        assert (await api.get(url(project, "draft"), headers=B)).status_code == 404
        assert (await api.post(url(project, "draft/cancel"), headers=B)).status_code == 404
        assert worker.tasks == []

    async def test_a_group_member_can_start_and_see_a_draft(self, api, worker, script):  # noqa: F811
        project = await make_project(api, share_with_groups=["team-1"])
        started = await api.post(url(project, "draft"), json=BODY, headers=B)
        assert started.status_code == 202
        status = (await api.get(url(project, "draft"), headers=A)).json()  # the owner sees the member's draft
        assert status["job"]["started_by"] == "user-b" and status["running"] is True
        worker.run_all()
        assert (await state_of(api, project, who=A))["counts"]["drafted"] == 3

    async def test_who_can_stop_a_draft(self, api, worker, script):  # noqa: F811
        project = await make_project(api, share_with_groups=["team-1"])
        member = as_user("user-m", "team-1")  # can open the project, but did not start the draft and does not own it
        await api.post(url(project, "draft"), json=BODY, headers=B)

        assert (await api.post(url(project, "draft/cancel"), headers=member)).status_code == 403  # neither starter nor owner
        stopped = await api.post(url(project, "draft/cancel"), headers=A)  # the project's owner
        assert stopped.status_code == 200 and stopped.json()["running"] is True  # it stops between steps

        worker.run_all()
        assert (await state_of(api, project))["counts"]["drafted"] == 0
        assert (await api.get(url(project, "draft"), headers=A)).json()["job"]["status"] == "cancelled"
        assert (await api.post(url(project, "draft/cancel"), headers=A)).status_code == 409  # nothing is running now

    async def test_an_administrator_can_stop_any_draft(self, api, worker, script):  # noqa: F811
        project = await make_project(api)
        await api.post(url(project, "draft"), json=BODY, headers=A)
        assert (await api.post(url(project, "draft/cancel"), headers=ADMIN)).status_code == 200

    async def test_asking_for_web_search_where_it_is_not_configured_is_refused(self, api, worker, script):  # noqa: F811
        project = await make_project(api)
        response = await api.post(url(project, "draft"), json={**BODY, "allow_web_search": True}, headers=A)
        assert response.status_code == 400 and worker.tasks == []

    @pytest.mark.parametrize(
        "body",
        [{}, {"client_id": "acme"}, {**BODY, "approach": "magic"}, {**BODY, "rids": ["bad id!"]}, {**BODY, "extra": 1}, {**BODY, "tone": "x" * 40}],
    )
    async def test_a_malformed_request_is_refused(self, api, worker, script, body):  # noqa: F811
        project = await make_project(api)
        assert (await api.post(url(project, "draft"), json=body, headers=A)).status_code == 422
        assert worker.tasks == []

    async def test_naming_a_requirement_that_does_not_exist_is_a_404(self, api, worker, script):  # noqa: F811
        project = await make_project(api)
        response = await api.post(url(project, "draft"), json={**BODY, "rids": ["R1", "R99"]}, headers=A)
        assert response.status_code == 404 and "R99" in response.json()["detail"]
        assert (await api.get(url(project, "draft"), headers=A)).json()["job"] is None  # no claim was left behind

    async def test_a_draft_that_cannot_start_gives_the_project_back(self, api, worker, script, use_settings):  # noqa: F811
        use_settings("easyauth", ledger_backend="memory", job_max_active_per_user=1)
        first = await make_project(api, name="first")
        second = await make_project(api, name="second")
        assert (await api.post(url(first, "draft"), json=BODY, headers=A)).status_code == 202

        limited = await api.post(url(second, "draft"), json=BODY, headers=A)

        assert limited.status_code == 429
        assert store().get(second["project"]["id"], "project").get("draft_job") is None  # not stuck "running"
        worker.run_all()
        use_settings("easyauth", ledger_backend="memory", job_max_active_per_user=1)
        assert (await api.post(url(second, "draft"), json=BODY, headers=A)).status_code == 202

    async def test_the_ledger_must_be_configured(self, client, use_settings):
        use_settings("easyauth")
        assert (await client.get(f"/projects/{'a' * 32}/draft", headers=A)).status_code == 503


# ── Pieces ───────────────────────────────────────────────


class TestPieces:
    def test_a_failed_generation_is_marked_so_nothing_stores_it_as_an_answer(self):
        failed = answers.error_answer({"id": "R1", "text": "t"}, message="Error: x", risk="x")
        assert failed["generation_failed"] is True and failed["state"] == "failed" and failed["meets"] == ""

    def test_strategies_carry_the_marker_and_the_whole_answer_through_the_section_approach(self, monkeypatch):
        good = {**reply_for({"id": "R1", "text": "t"}), "extra": "dropped"}
        monkeypatch.setattr(answers, "answer_single_requirement", lambda **kw: good if kw["requirement"]["id"] == "R1" else answers.error_answer(kw["requirement"], message="Error", risk="r"))
        monkeypatch.setattr(strategies.kb_search, "retrieve_for_requirements", lambda reqs, **kw: [strategies.RetrievedContext() for _ in reqs])
        monkeypatch.setattr(strategies.sections, "classify_sections", lambda *a, **k: {"S": ["R1", "R2"]})
        monkeypatch.setattr(strategies.sections, "write_sections", lambda *a, **k: iter(()))
        monkeypatch.setattr(strategies.lifecycle, "ensure_agent", lambda name: "agent")

        events = list(strategies.stream_per_section(
            requirements=[{"id": "R1", "text": "t"}, {"id": "R2", "text": "t"}], template_sections=[], client_id="c",
            project_id="p", user_oid="u",
        ))

        data = {e["data"]["requirement_id"]: e["data"] for e in events if e["type"] == "requirement_compliance"}
        assert data["R1"]["risks"] == ["r"] and data["R1"]["kb_evidence_found"] is True and "extra" not in data["R1"]
        assert data["R2"]["generation_failed"] is True and "generation_failed" not in data["R1"]

    def test_the_selection_is_the_unanswered_requirements_in_document_order(self):
        memory = ledger_store.MemoryStore()
        docs = [
            {"id": f"req:R{i}", "kind": "requirement", "projectId": "p", "rid": f"R{i}", "order": i, "text": f"t{i}",
             "rev": 1, "answer_version": 1 if i == 2 else 0}
            for i in (3, 1, 2)
        ]
        memory.apply("p", [ledger_store.create(d) for d in docs])
        selection = drafting.select(memory, "p", None)
        assert [r["id"] for r in selection.requirements] == ["R1", "R3"] and selection.revs == {"R1": 1, "R3": 1}
        assert [r["id"] for r in drafting.select(memory, "p", ["R2", "R1"]).requirements] == ["R1", "R2"]

    def test_a_sink_ignores_an_answer_it_did_not_ask_for(self, caplog):
        memory = ledger_store.MemoryStore()
        sink = drafting.ProjectSink(memory, api_settings(), "p", "job", drafting.Selection([], {}))
        assert sink.record({"requirement_id": "R9", "answer_markdown": "x"}) is None

    async def test_a_retried_write_that_had_gone_through_counts_as_ours(self, api, worker, script, monkeypatch):  # noqa: F811
        """The first attempt succeeds in the database but the reply is lost: the retry finds its own write."""
        project = await make_project(api)
        project_id = project["project"]["id"]
        real = store().apply
        lost = {"done": False}

        def reply_lost(pid, ops):
            real(pid, ops)
            if not lost["done"] and any(o.doc.get("kind") == "answer" for o in ops):
                lost["done"] = True
                raise HttpResponseError(message="timeout after the write")

        monkeypatch.setattr(drafting.time, "sleep", lambda _s: None)
        monkeypatch.setattr(store(), "apply", reply_lost)
        await draft(api, worker, project)

        state = await state_of(api, project)
        assert [r["answer_version"] for r in state["requirements"]] == [1, 1, 1]  # not written twice
        assert KB_BLOB and REQUIREMENTS and project_id
