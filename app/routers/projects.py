"""The project ledger: projects, their requirements, versioned answers and the audit trail."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query

from app.auth.entra import UserClaims, get_current_user
from app.auth.ratelimit import rate_limited
from app.config import Settings, get_settings
from app.ledger import service
from app.ledger.schemas import AnswerCreate, ProjectCreate, ProjectUpdate, RequirementUpdate
from app.ledger.store import LedgerStore, store_for
from app.services.drafts import load_rfp, prepare_draft

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
