"""Drafting a project: answers are saved to the ledger as they are produced, and a draft picks up where it left off.

A project draft is an ordinary background draft job (see :mod:`app.services.jobs`) with a *sink*: each answer the model
produces is written to the ledger as a new answer version *before* it is sent on, so

* a cancelled, failed or interrupted draft keeps everything it had finished;
* starting a draft again drafts only the requirements that still have no answer (finished work is never redone, and
  an answer a person wrote or approved is never replaced unless that requirement is named explicitly);
* an answer a person edits while the draft is running is not overwritten by the model's later answer for it;
* a requirement whose generation failed gets no answer, so the next run tries it again.

Only one draft runs per project at a time: starting one claims the project with an etag-protected write, so two people
pressing the button together start one job, not two.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from azure.core.exceptions import HttpResponseError, ServiceRequestError
from fastapi import HTTPException

from app.agents import confidence, states, verify
from app.auth.access import can_manage
from app.auth.entra import UserClaims
from app.config import Settings
from app.ledger import service
from app.ledger import store as ledger_store
from app.ledger.store import Conflict, Doc, LedgerStore
from app.schemas import DraftRequest
from app.services import jobs

logger = logging.getLogger(__name__)

RETRIES = 3


@dataclass
class Selection:
    """The requirements a draft will work on, shaped for the drafting strategies, and the revision of each."""

    requirements: list[dict[str, Any]]
    revs: dict[str, int] = field(default_factory=dict)


def _strategy_requirement(doc: Doc) -> dict[str, Any]:
    return {
        "id": doc["rid"],
        "text": doc["text"],
        "type": doc.get("type", "requirement"),
        "category": doc.get("category", "general"),
        "priority": doc.get("priority", "standard"),
        "source_section": doc.get("source_section", ""),
        "kind": doc.get("item_kind") or ("question" if doc.get("type") == "question" else "mandatory"),
        "response_type": doc.get("response_type", ""),
    }


def select(store: LedgerStore, project_id: str, rids: list[str] | None) -> Selection:
    """What to draft: the requirements without an answer, or exactly the ones named (redoing any that have one)."""
    docs = store.query(project_id, "requirement", order_by="order")
    if rids:
        by_rid = {d["rid"]: d for d in docs}
        unknown = [r for r in rids if r not in by_rid]
        if unknown:
            raise HTTPException(status_code=404, detail=f"No such requirement: {', '.join(unknown[:5])}")
        chosen = [d for d in docs if d["rid"] in set(rids)]
    else:
        # What a draft works on by default: answered kinds without an answer, and not a repeat (it takes the first
        # one's answer). Instructions, dates and submission rules are not drafted.
        chosen = [d for d in docs if not d.get("answer_version") and service.is_answered(d) and not service.is_repeat(d)]
    if not chosen:
        raise HTTPException(status_code=409, detail="Nothing to draft: every requirement already has an answer.")
    return Selection([_strategy_requirement(d) for d in chosen], {d["rid"]: d["rev"] for d in chosen})


# ── The sink ─────────────────────────────────────────────


class ProjectSink:
    """Receives each answer of a running draft and writes it to the ledger (see the module docstring)."""

    def __init__(self, store: LedgerStore, settings: Settings, project_id: str, job_id: str, selection: Selection) -> None:
        self._store, self._settings = store, settings
        self._project_id, self._job_id = project_id, job_id
        self._revs = dict(selection.revs)
        self.reused: dict[str, list[str]] = {}  # first item -> the repeats that took its answer
        self.written: list[str] = []
        self.changed: list[str] = []  # not saved: somebody edited the requirement while the draft ran
        self.failed: list[str] = []  # not saved: the model could not produce an answer

    def _not_saved(self, rid: str, reason: str) -> dict[str, Any]:
        return {"type": "not_saved", "data": {"requirement_id": rid, "reason": reason}}

    def record(self, response: dict[str, Any]) -> dict[str, Any] | None:
        rid = str(response.get("requirement_id", ""))
        if rid not in self._revs:
            logger.warning("Draft %s produced an answer for %r, which it was not asked about", self._job_id, rid)
            return None
        if response.get("generation_failed"):
            self.failed.append(rid)
            self._mark_failed(rid)
            return self._not_saved(rid, "failed")

        for attempt in range(1, RETRIES + 1):
            try:
                requirement = service.add_answer(
                    self._store, self._settings, self._project_id, rid, response,
                    actor=service.AGENT, source="model", job_id=self._job_id, expected_rev=self._revs[rid],
                )
            except HTTPException as exc:
                if exc.status_code != 409:
                    raise
                if self._is_ours(rid):  # a retry of a write that had in fact gone through
                    self.written.append(rid)
                    return {"type": "saved", "data": {"requirement_id": rid, "version": self._current_version(rid)}}
                self.changed.append(rid)
                logger.info("Draft %s did not save %s: it was changed while the draft ran", self._job_id, rid)
                return self._not_saved(rid, "changed")
            except (HttpResponseError, ServiceRequestError):
                if attempt == RETRIES:
                    raise
                logger.warning("Ledger write for %s failed (attempt %d); retrying", rid, attempt, exc_info=True)
                time.sleep(0.5 * attempt)
            else:
                self.written.append(rid)
                reused = self._reuse(rid, response)
                return {"type": "saved", "data": {"requirement_id": rid, "version": requirement["answer_version"],
                                                  **({"reused_by": reused} if reused else {})}}
        return None  # pragma: no cover - the loop always returns or raises

    def _reuse(self, rid: str, response: dict[str, Any]) -> list[str]:
        """Give each repeat of *rid* the answer it just got, so one question is answered once and consistently.

        A repeat that a person has answered, edited or approved keeps their answer; a repeat whose answer is itself a
        model's (an earlier reuse, or one drafted before it was grouped) is replaced.
        """
        taken: list[str] = []
        for member in self._store.query(self._project_id, "requirement", order_by="order"):
            if member.get("duplicate_of") != rid:
                continue
            answer = member.get("answer") or {}
            if member.get("answer_version") and (answer.get("source") != "model" or member.get("status") == "approved"):
                continue
            try:
                service.add_answer(
                    self._store, self._settings, self._project_id, member["rid"], {**response, "reused_from": rid},
                    actor=service.AGENT, source="model", job_id=self._job_id, expected_rev=member["rev"],
                )
            except HTTPException:
                logger.info("Draft %s did not copy %s to %s: it changed meanwhile", self._job_id, rid, member["rid"])
                continue
            taken.append(member["rid"])
        if taken:
            self.reused[rid] = taken
        return taken

    def _mark_failed(self, rid: str) -> None:
        """Show the requirement as failed (best effort: a conflict means somebody just changed it, which wins)."""
        try:
            service.mark_failed(self._store, self._project_id, rid, job_id=self._job_id, expected_rev=self._revs[rid])
        except HTTPException:
            pass
        except (HttpResponseError, ServiceRequestError):
            logger.warning("Could not record that %s failed", rid, exc_info=True)

    def _is_ours(self, rid: str) -> bool:
        doc = self._store.get(self._project_id, service.req_doc_id(rid))
        return bool(doc and (doc.get("answer") or {}).get("job_id") == self._job_id)

    def _current_version(self, rid: str) -> int:
        doc = self._store.get(self._project_id, service.req_doc_id(rid))
        return int(doc["answer_version"]) if doc else 0

    def document_inputs(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Everything the project has: its documents are the whole bid, not just what this run drafted."""
        docs = self._store.query(self._project_id, "requirement", order_by="order")
        responses = [
            response_of(d) if d.get("answer") else states.failed_answer(_strategy_requirement(d), message="Drafting failed", risk="")
            for d in docs
            if d.get("answer") or d.get("state") == states.FAILED
        ]
        return responses, [_strategy_requirement(d) for d in docs]

    def finish(self, outcome: str) -> None:
        event = service.new_event(
            self._project_id, service.AGENT, "draft.finished",
            job_id=self._job_id, outcome=outcome, saved=len(self.written),
            changed_meanwhile=len(self.changed), failed=len(self.failed), reused=sum(len(v) for v in self.reused.values()),
        )
        self._store.apply(self._project_id, [ledger_store.create(event)])


def response_of(requirement: Doc) -> dict[str, Any]:
    """A requirement's current answer in the shape the document writers take."""
    answer = requirement["answer"]
    return {
        "requirement_id": requirement["rid"],
        "requirement_text": requirement["text"],
        "answer_markdown": answer.get("answer_markdown", ""),
        "meets": answer.get("meets", ""),
        "evidence": answer.get("evidence", []),
        "assumptions": answer.get("assumptions", []),
        "risks": answer.get("risks", []),
        "followups": answer.get("followups", []),
        "kb_evidence_found": answer.get("kb_evidence_found", False),
        "state": requirement.get("state", ""),
        "missing": requirement.get("missing", ""),
        "claims": answer.get("claims", []),
        "reused_from": answer.get("reused_from", ""),
        "verification": answer.get("verification", {}),
        "confidence": answer.get("confidence", {}),
    }


# ── Starting, watching and stopping a draft ──────────────


CLAIM_GRACE_SECONDS = 60  # how long a claim with no job record yet is trusted: submitting the job takes a moment


def _claim_is_young(claim: dict[str, Any]) -> bool:
    try:
        started = datetime.fromisoformat(str(claim.get("at", "")).replace("Z", "+00:00"))
    except ValueError:
        return False
    return (datetime.now(UTC) - started).total_seconds() < CLAIM_GRACE_SECONDS


def _active_job(settings: Settings, project: Doc) -> jobs.Job | None:
    claim = project.get("draft_job")
    job = jobs.find_job(settings, claim["id"]) if claim else None
    return job if job and job.status in jobs.ACTIVE_STATES else None


def _is_starting(settings: Settings, project: Doc) -> bool:
    """A draft has claimed the project but its job record is not there yet (it is being submitted).

    Without this a second request arriving in that moment would see "no job", claim over the first and start a
    second draft of the same requirements.
    """
    claim = project.get("draft_job")
    return bool(claim and jobs.find_job(settings, claim["id"]) is None and _claim_is_young(claim))


def start(
    store: LedgerStore,
    settings: Settings,
    user: UserClaims,
    project_id: str,
    *,
    request: DraftRequest,
    rids: list[str] | None,
    allow_web_search: bool,
) -> jobs.Job:
    """Start a draft of the project's unanswered requirements (or the named ones). 409 if one is already running."""
    project = service.load_project(store, project_id, user, settings)
    if not (project.get("extraction") or {}).get("confirmed", True):
        raise HTTPException(
            status_code=409,
            detail="Review the requirements the model extracted and confirm them first: it can misread an RFP.",
        )
    running = _active_job(settings, project)
    if running:
        raise HTTPException(status_code=409, detail=f"A draft is already running for this project (job {running.id}).")
    if _is_starting(settings, project):
        raise HTTPException(status_code=409, detail="A draft is just starting for this project.")

    selection = select(store, project_id, rids)
    job_id = uuid.uuid4().hex
    actor = service.Actor.of(user)
    project["draft_job"] = {"id": job_id, "by": user.oid, "at": service._now(), "requirements": len(selection.requirements)}
    try:
        store.apply(
            project_id,
            [
                ledger_store.replace(project),
                ledger_store.create(
                    service.new_event(
                        project_id, actor, "draft.started", job_id=job_id, requirements=len(selection.requirements),
                        approach=request.approach, redo=bool(rids),
                    )
                ),
            ],
        )
    except Conflict:
        raise HTTPException(status_code=409, detail="A draft was just started for this project.") from None

    sink = ProjectSink(store, settings, project_id, job_id, selection)
    try:
        return jobs.submit(
            settings, user, request, selection.requirements, allow_web_search, project.get("guidance", ""),
            sink=sink, project_id=project_id, job_id=job_id,
        )
    except Exception:
        _release(store, project_id, job_id)
        raise


def _release(store: LedgerStore, project_id: str, job_id: str) -> None:
    """Give the project back after a draft that never started (best effort)."""
    try:
        project = store.get(project_id, "project")
        if project and (project.get("draft_job") or {}).get("id") == job_id:
            project.pop("draft_job")
            store.apply(project_id, [ledger_store.replace(project)])
    except Exception:
        logger.exception("Could not release project %s after a failed draft start", project_id)


def status(store: LedgerStore, settings: Settings, user: UserClaims, project_id: str) -> dict[str, Any]:
    """Where the project's drafting stands: the latest job, and how much is still to do."""
    project = service.load_project(store, project_id, user, settings)
    requirements = store.query(project_id, "requirement")
    remaining = sum(1 for r in requirements if not r.get("answer_version") and service.is_answered(r) and not service.is_repeat(r))
    needs_input = sum(1 for r in requirements if r.get("status") == "needs_input")
    failed = sum(1 for r in requirements if r.get("state") == states.FAILED and not r.get("answer_version"))
    flagged = sum(1 for r in requirements if r.get("verification") == verify.FLAGGED)
    low_confidence = sum(1 for r in requirements if r.get("confidence") == confidence.LOW and r.get("answer_version"))
    claim = project.get("draft_job")
    job = jobs.find_job(settings, claim["id"]) if claim else None

    summary = None
    if job:
        summary = {
            "job_id": job.id,
            "status": job.status,
            "approach": job.approach,
            "started_by": claim.get("by", ""),
            "total": job.total,
            "processed": job.processed,
            "error": job.error,
            "created_at": job.created_at,
            "updated_at": job.updated_at,
        }
        if job.status == "succeeded" and job.outputs:
            summary["downloads"] = {
                "docx": f"/outputs/{project_id}/draft.docx",
                "xlsx": f"/outputs/{project_id}/compliance.xlsx",
            }
    return {
        "job": summary,
        "running": bool(job and job.status in jobs.ACTIVE_STATES) or _is_starting(settings, project),
        "interrupted": bool(job and job.status == "failed" and job.error == jobs.LOST_JOB),
        "remaining": remaining,
        "needs_input": needs_input,
        "failed": failed,
        "flagged": flagged,
        "low_confidence": low_confidence,
        "total": len(requirements),
    }


def cancel(store: LedgerStore, settings: Settings, user: UserClaims, project_id: str) -> dict[str, Any]:
    """Ask the running draft to stop (the person who started it, the project's owner, or an administrator)."""
    project = service.load_project(store, project_id, user, settings)
    job = _active_job(settings, project)
    if job is None:
        if _is_starting(settings, project):
            raise HTTPException(status_code=409, detail="The draft is still starting; try again in a moment.")
        raise HTTPException(status_code=409, detail="No draft is running for this project.")
    if user.oid != job.owner_oid and not can_manage(project.get("owner"), user, settings):
        raise HTTPException(status_code=403, detail="Only the person who started the draft, or the project's owner, can stop it")
    jobs.request_cancel(settings, job)
    return status(store, settings, user, project_id)
