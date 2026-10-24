"""The project ledger: projects, their requirements, versioned answers and the audit trail."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query

from app.auth.entra import UserClaims, get_current_user
from app.auth.ratelimit import rate_limited
from app.config import Settings, get_settings
from app.ledger import drafting, service
from app.ledger.schemas import AnswerCreate, ProjectCreate, ProjectDraft, ProjectUpdate, RequirementUpdate
from app.ledger.store import LedgerStore, store_for
from app.services.drafts import load_rfp, prepare_draft, web_search_choice

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/projects", tags=["projects"])

ProjectId = Annotated[str, Path(pattern=service.PROJECT_ID_PATTERN)]
RequirementId = Annotated[str, Path(pattern=service.RID_PATTERN)]


def ledger(settings: Settings = Depends(get_settings)) -> LedgerStore:
    store = store_for(settings)
    if store is None:
        raise HTTPException(
            status_code=503,
            detail="The project ledger isn't configured on this deployment (set COSMOS_ENDPOINT).",
        )
    return store


@router.post("", status_code=201, dependencies=[Depends(rate_limited)])
def create_project(
    body: ProjectCreate,
    user: UserClaims = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
    store: LedgerStore = Depends(ledger),
):
    """Start a project from an RFP you uploaded (its requirements are extracted) or from requirements you send.

    The new project is private to you unless ``share_with_groups`` names groups you belong to. Returns the whole
    project (requirements included).
    """
    guidance = ""
    requirements = list(body.requirements or [])
    if body.rfp_id:
        rfp = load_rfp(settings, user, body.rfp_id, body.filename)  # 404 unless the RFP is yours
        inputs = prepare_draft(requirements, rfp)
        requirements, guidance = inputs.requirements, inputs.guidance
    return service.create_project(
        store,
        settings,
        user,
        name=body.name,
        requirements=requirements,
        rfp_id=body.rfp_id,
        guidance=guidance,
        groups=body.share_with_groups,
    )


@router.get("")
def list_projects(
    user: UserClaims = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
    store: LedgerStore = Depends(ledger),
):
    """The projects you own or that are shared with one of your groups, newest first."""
    projects = service.list_projects(store, user, settings)
    return {"projects": projects, "count": len(projects)}


@router.get("/{project_id}")
def get_project(
    project_id: ProjectId,
    user: UserClaims = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
    store: LedgerStore = Depends(ledger),
):
    """The project, its requirements with their current answers, and counts by status. 404 if you can't open it."""
    return service.get_state(store, project_id, user, settings)


@router.patch("/{project_id}", dependencies=[Depends(rate_limited)])
def update_project(
    project_id: ProjectId,
    body: ProjectUpdate,
    user: UserClaims = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
    store: LedgerStore = Depends(ledger),
):
    """Rename a project or change who it is shared with (owner or administrator)."""
    return service.update_project(
        store, settings, user, project_id, name=body.name, groups=body.share_with_groups
    )


@router.delete("/{project_id}", dependencies=[Depends(rate_limited)])
def delete_project(
    project_id: ProjectId,
    user: UserClaims = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
    store: LedgerStore = Depends(ledger),
):
    """Delete a project with all its requirements, answer versions and history (owner or administrator)."""
    return {"project_id": project_id, "deleted_documents": service.delete_project(store, settings, user, project_id)}


@router.get("/{project_id}/requirements/{rid}")
def get_requirement(
    project_id: ProjectId,
    rid: RequirementId,
    user: UserClaims = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
    store: LedgerStore = Depends(ledger),
):
    return service.get_requirement(store, project_id, rid, user, settings)


@router.patch("/{project_id}/requirements/{rid}", dependencies=[Depends(rate_limited)])
def update_requirement(
    project_id: ProjectId,
    rid: RequirementId,
    body: RequirementUpdate,
    user: UserClaims = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
    store: LedgerStore = Depends(ledger),
):
    """Edit a requirement (text, category, priority, assignee, status). Send ``expected_rev`` (the `rev` you saw) to
    be refused with 409 if somebody else changed it meanwhile. Every edit is attributed and kept in the history."""
    return service.update_requirement(
        store, settings, user, project_id, rid, body.changes(), expected_rev=body.expected_rev
    )


@router.get("/{project_id}/requirements/{rid}/answers")
def list_answers(
    project_id: ProjectId,
    rid: RequirementId,
    user: UserClaims = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
    store: LedgerStore = Depends(ledger),
):
    """Every version of the requirement's answer, newest first."""
    answers = service.list_answers(store, project_id, rid, user, settings)
    return {"answers": answers, "count": len(answers)}


@router.post("/{project_id}/requirements/{rid}/answers", status_code=201, dependencies=[Depends(rate_limited)])
def add_answer(
    project_id: ProjectId,
    rid: RequirementId,
    body: AnswerCreate,
    user: UserClaims = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
    store: LedgerStore = Depends(ledger),
):
    """Write a new answer version, or make an earlier one current again (``restore_version``). Earlier versions are
    kept; the requirement becomes ``edited`` and any approval is withdrawn."""
    if body.restore_version is not None:
        return service.restore_answer(
            store, settings, user, project_id, rid, body.restore_version, expected_rev=body.expected_rev
        )
    service.load_project(store, project_id, user, settings)
    return service.add_answer(
        store,
        settings,
        project_id,
        rid,
        body.content(),
        actor=service.Actor.of(user),
        source="user",
        expected_rev=body.expected_rev,
    )


@router.get("/{project_id}/events")
def list_events(
    project_id: ProjectId,
    rid: Annotated[str | None, Query(pattern=service.RID_PATTERN, description="Only this requirement's history")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0, le=100000)] = 0,
    user: UserClaims = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
    store: LedgerStore = Depends(ledger),
):
    """The audit trail: who changed what and when, newest first."""
    events = service.list_events(store, project_id, user, settings, rid=rid, limit=limit, offset=offset)
    return {"events": events, "count": len(events), "next_offset": offset + len(events)}


@router.get("/{project_id}/calibration")
def calibration(
    project_id: ProjectId,
    user: UserClaims = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
    store: LedgerStore = Depends(ledger),
):
    """How each confidence band has fared with reviewers: answers accepted as they were, edited, or still pending."""
    return service.calibration(store, project_id, user, settings)


# ── Drafting ─────────────────────────────────────────────


@router.post("/{project_id}/draft", status_code=202, dependencies=[Depends(rate_limited)])
def start_draft(
    project_id: ProjectId,
    body: ProjectDraft,
    user: UserClaims = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
    store: LedgerStore = Depends(ledger),
):
    """Draft the project's requirements in the background, saving each answer to the ledger as it is produced.

    Only requirements without an answer are drafted (name some in ``rids`` to redo them), so after an interruption
    this picks up where it left off. One draft runs per project at a time (409 otherwise). Poll
    ``GET /projects/{id}/draft`` or just reload the project: answers appear in it as they are written.
    """
    allow_web_search = web_search_choice(body.allow_web_search, settings)
    job = drafting.start(
        store, settings, user, project_id,
        request=body.as_request(project_id), rids=body.rids, allow_web_search=allow_web_search,
    )
    return {"job_id": job.id, "status": job.status, "total": job.total, "status_url": f"/projects/{project_id}/draft"}


@router.get("/{project_id}/draft")
def draft_status(
    project_id: ProjectId,
    user: UserClaims = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
    store: LedgerStore = Depends(ledger),
):
    """The latest draft's status (``running``, ``interrupted``), what is still unanswered and where to download the
    documents. Anyone who can open the project can see it."""
    return drafting.status(store, settings, user, project_id)


@router.post("/{project_id}/draft/cancel", dependencies=[Depends(rate_limited)])
def cancel_draft(
    project_id: ProjectId,
    user: UserClaims = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
    store: LedgerStore = Depends(ledger),
):
    """Ask the running draft to stop. Answers already saved are kept."""
    return drafting.cancel(store, settings, user, project_id)
