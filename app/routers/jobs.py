"""Background draft jobs: start a draft, poll its progress, cancel it."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response

from app.auth.entra import UserClaims, get_current_user
from app.auth.ratelimit import rate_limited
from app.config import Settings, get_settings
from app.schemas import DraftRequest
from app.services import jobs
from app.services.drafts import load_rfp, prepare_draft, web_search_choice
from app.validation import JobId

router = APIRouter(prefix="/drafts", tags=["agent"])


def _summary(job: jobs.Job) -> dict:
    return {
        "job_id": job.id,
        "status": job.status,
        "approach": job.approach,
        "rfp_id": job.rfp_id,
        "total": job.total,
        "processed": job.processed,
        "outputs": job.outputs,
        "error": job.error,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }


@router.post("", status_code=202, dependencies=[Depends(rate_limited)])
def start_draft(
    req: DraftRequest,
    response: Response,
    user: UserClaims = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    """Start a draft in the background and return its job id (``202 Accepted``).

    Takes the same body as ``POST /draft/stream``. Poll ``GET /drafts/{job_id}`` for progress; unlike the
    request-bound endpoints, the draft is not cut off by the platform's 240-second request timeout.
    """
    allow_web_search = web_search_choice(req.allow_web_search, settings)
    rfp = load_rfp(settings, user, req.rfp_id)
    inputs = prepare_draft(req.requirements, rfp)

    job = jobs.submit(settings, user, req, inputs.requirements, allow_web_search, inputs.guidance)
    response.headers["Location"] = f"/drafts/{job.id}"
    return {"job_id": job.id, "status": job.status, "status_url": f"/drafts/{job.id}"}


@router.get("/{job_id}")
def get_draft(
    job_id: JobId,
    after: int = Query(0, ge=0, description="Return only events from this position on (the previous `next`)."),
    user: UserClaims = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    """A job's status and the events produced since ``after``.

    ``status`` is ``queued``, ``running``, ``succeeded``, ``failed`` or ``cancelled``. The events are the same ones
    ``/draft/stream`` sends (``start``, ``response``, ``section_narrative``, ..., ``complete``). Poll with
    ``after`` set to the ``next`` of the previous reply until the status is final.
    """
    job = jobs.get_job(settings, user, job_id)
    return {**_summary(job), "events": job.events[after:], "next": len(job.events)}


@router.post("/{job_id}/cancel", status_code=202)
def cancel_draft(
    job_id: JobId,
    user: UserClaims = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    """Ask a running draft to stop. It stops between steps, so poll to see when it has."""
    return _summary(jobs.cancel_job(settings, user, job_id))
