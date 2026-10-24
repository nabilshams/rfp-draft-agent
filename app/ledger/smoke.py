"""Check the ledger against the real database: ``python -m app.ledger.smoke``.

Run it inside the deployed container (the database is reachable only from there):

    az containerapp exec -n <app> -g <resource-group> --command "python -m app.ledger.smoke"

It creates a throw-away project, exercises batches, versions, optimistic concurrency, queries and deletion, and
removes the project again. It prints one line per check and exits 1 on the first failure.
"""

from __future__ import annotations

import sys

from fastapi import HTTPException

from app.auth.entra import UserClaims
from app.config import get_settings
from app.ledger import drafting, service
from app.ledger.store import store_for


def main() -> int:
    settings = get_settings()
    store = store_for(settings)
    if store is None:
        print("FAIL the ledger is not configured (COSMOS_ENDPOINT is empty)")
        return 1
    user = UserClaims(oid="ledger-smoke-test", name="Ledger smoke test", groups=[])
    project_id = ""

    def check(name: str, ok: bool) -> None:
        print(("ok   " if ok else "FAIL ") + name)
        if not ok:
            raise SystemExit(1)

    try:
        store.ping()
        check("connects and reads the container", True)

        rows = [{"id": f"R{i}", "text": f"The vendor shall do thing number {i}.", "anchor": f"¶{i}"} for i in range(1, 131)]
        state = service.create_project(store, settings, user, name="smoke test", requirements=rows)
        project_id = state["project"]["id"]
        check("creates a project with 130 requirements across two batches", len(state["requirements"]) == 130)

        check("lists the project for its owner", project_id in [p["id"] for p in service.list_projects(store, user, settings)])

        stranger = UserClaims(oid="someone-else", groups=[])
        try:
            service.get_state(store, project_id, stranger, settings)
            hidden = False
        except HTTPException as exc:
            hidden = exc.status_code == 404
        check("hides the project from someone else (404)", hidden)

        updated = service.update_requirement(store, settings, user, project_id, "R1", {"category": "security"}, expected_rev=1)
        check("edits a requirement (rev 2)", updated["rev"] == 2 and updated["category"] == "security")

        try:
            service.update_requirement(store, settings, user, project_id, "R1", {"category": "x"}, expected_rev=1)
            refused = False
        except HTTPException as exc:
            refused = exc.status_code == 409
        check("refuses a stale edit (409)", refused)

        service.add_answer(store, settings, project_id, "R1", {"answer_markdown": "One."}, actor=service.Actor.of(user), source="user")
        service.add_answer(store, settings, project_id, "R1", {"answer_markdown": "Two."}, actor=service.Actor.of(user), source="user")
        versions = service.list_answers(store, project_id, "R1", user, settings)
        check("keeps answer versions newest first", [v["version"] for v in versions] == [2, 1])

        events = service.list_events(store, project_id, user, settings, limit=3)
        check("returns the audit trail newest first, paged", len(events) == 3 and events[0]["action"] == "answer.added")

        # The path a background draft uses: pick the unanswered requirements, save each answer as it arrives
        selection = drafting.select(store, project_id, None)
        check("selects only the requirements without an answer", len(selection.requirements) == 129 and "R1" not in selection.revs)
        sink = drafting.ProjectSink(store, settings, project_id, "smoke-job", selection)
        saved = sink.record({"requirement_id": "R2", "answer_markdown": "Drafted.", "meets": "Yes", "evidence": [{"id": "E1", "title": "t", "source_url": "/kb/doc/x", "quote": "q", "page": 3, "quote_verified": True}], "claims": [{"id": "C1", "text": "It is drafted.", "evidence_ids": ["E1"], "verdict": "unsupported", "reason": "r", "checked_by": "rule"}], "verification": {"status": "flagged", "claims": 1, "unsupported": 1}})
        check("saves a model answer as it arrives", saved["type"] == "saved" and saved["data"]["version"] == 1)
        service.update_requirement(store, settings, user, project_id, "R3", {"category": "edited-by-a-person"})
        refused = sink.record({"requirement_id": "R3", "answer_markdown": "Too late."})
        check("does not overwrite a requirement a person changed meanwhile", refused["data"]["reason"] == "changed")
        failed = sink.record({"requirement_id": "R4", "answer_markdown": "Error", "generation_failed": True})
        check("does not store a failed generation as an answer", failed["data"]["reason"] == "failed")
        check("marks the requirement as failed", service.get_requirement(store, project_id, "R4", user, settings)["state"] == "failed")
        responses, requirements = sink.document_inputs()
        check("builds the documents from every answer, and every failure, in the project", len(responses) == 3 and len(requirements) == 130)
        stored = service.get_requirement(store, project_id, "R2", user, settings)
        check("keeps the claims and their verdicts with the answer", stored["verification"] == "flagged" and stored["answer"]["claims"][0]["evidence_ids"] == ["E1"])
        check("keeps evidence details on the answer", service.get_requirement(store, project_id, "R2", user, settings)["answer"]["evidence"][0]["page"] == 3)
    finally:
        if project_id:
            removed = store.delete_project(project_id)
            print(f"ok   deleted the project ({removed} documents)")
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
