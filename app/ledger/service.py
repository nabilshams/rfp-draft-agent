"""Operations on the project ledger.

A *project* is one bid: it has an owner (and may be shared with groups the owner belongs to), a list of
*requirements* (each with a stable ID, an anchor in the RFP, an owner and a status), versioned *answers*, and an
append-only *audit trail*. Anyone who can open the project can read it and edit its requirements and answers; only
the owner (or an administrator) can rename, share or delete it.

Every change writes the changed requirement, any new answer version and the audit event in one atomic batch, and is
refused (409) if somebody else changed the requirement in the meantime, so two people cannot silently overwrite each
other.
"""

from __future__ import annotations

import logging
import re
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException

from app.agents.normalize import normalize_answer
from app.auth.access import can_manage, is_admin, resolve_share_groups
from app.auth.entra import UserClaims
from app.config import Settings
from app.ingestion.rfp_extract import Requirement, assign_ids
from app.ledger import store as ledger_store
from app.ledger.store import Conflict, Doc, LedgerStore, public

logger = logging.getLogger(__name__)

STATUSES = ("extracted", "drafted", "edited", "approved")
PRIORITIES = ("mandatory", "preferred", "standard")
MAX_NAME = 200
MAX_TEXT = 5000

_ID_CHARS = re.compile(r"[^A-Za-z0-9._-]+")
RID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
PROJECT_ID_PATTERN = r"^[0-9a-f]{32}$"


@dataclass(frozen=True)
class Actor:
    oid: str
    name: str = ""

    @classmethod
    def of(cls, user: UserClaims) -> Actor:
        return cls(user.oid, user.name or user.preferred_username)


AGENT = Actor("agent", "RFP Draft Agent")


_clock_lock = threading.Lock()
_last_tick = datetime.min.replace(tzinfo=UTC)


def _now() -> str:
    """The current time, to the microsecond and never the same twice in this process, so that events written one
    after the other always sort in the order they happened."""
    global _last_tick
    with _clock_lock:
        tick = datetime.now(UTC)
        if tick <= _last_tick:
            tick = _last_tick + timedelta(microseconds=1)
        _last_tick = tick
    return tick.isoformat(timespec="microseconds").replace("+00:00", "Z")


def ledger_id(raw: str) -> str:
    """A requirement ID that is safe in a URL and a document ID: letters, digits, '.', '_' and '-'."""
    cleaned = _ID_CHARS.sub("-", raw.strip()).strip("-.")
    return cleaned[:64] or "R"


def req_doc_id(rid: str) -> str:
    return f"req:{rid}"


def _answer_doc_id(rid: str, version: int) -> str:
    return f"ans:{rid}:{version:05d}"


# ── Access ───────────────────────────────────────────────


def can_view(project: Doc, user: UserClaims, settings: Settings) -> bool:
    return (
        is_admin(user, settings)
        or project.get("owner") == user.oid
        or bool(set(project.get("groups", [])) & set(user.groups))
    )


def load_project(store: LedgerStore, project_id: str, user: UserClaims, settings: Settings) -> Doc:
    """The project if the user may open it; 404 for a project that doesn't exist and for one they can't see."""
    project = store.get(project_id, "project")
    if project is None or not can_view(project, user, settings):
        if project is not None:
            logger.warning("User %s denied access to project %s", user.oid, project_id)
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def _require_manage(project: Doc, user: UserClaims, settings: Settings) -> None:
    if not can_manage(project.get("owner"), user, settings):
        raise HTTPException(status_code=403, detail="Only the project's owner or an administrator can do this")


def _conflict() -> HTTPException:
    return HTTPException(status_code=409, detail="Someone else changed this just now. Reload and try again.")


# ── Events ───────────────────────────────────────────────


def new_event(project_id: str, actor: Actor, action: str, rid: str | None = None, **details: Any) -> Doc:
    at = _now()
    event: Doc = {
        "id": f"evt:{at}:{uuid.uuid4().hex[:8]}",
        "kind": "event",
        "projectId": project_id,
        "at": at,
        "actor": actor.oid,
        "actor_name": actor.name,
        "action": action,
        "details": details,
    }
    if rid:
        event["rid"] = rid
    return event


# ── Projects ─────────────────────────────────────────────


def _requirement_docs(project_id: str, requirements: list[dict[str, Any]], actor: Actor, now: str) -> list[Doc]:
    """Ledger documents for extracted or supplied requirements: unique clean IDs, in document order."""
    items = [
        Requirement(
            id=ledger_id(str(r.get("id", ""))) if str(r.get("id", "")).strip() else "",
            text=str(r.get("text", "")).strip()[:MAX_TEXT],
            type="question" if str(r.get("type", "")) == "question" else "requirement",
            category=str(r.get("category") or "general")[:64],
            priority=str(r.get("priority") or "standard").lower(),
            source_section=str(r.get("source_section") or "")[:200],
            anchor=str(r.get("anchor") or "")[:100],
            keywords=[str(k)[:64] for k in (r.get("keywords") or [])][:10],
        )
        for r in requirements
        if str(r.get("text", "")).strip()
    ]
    assign_ids(items)
    return [
        {
            "id": req_doc_id(item.id),
            "kind": "requirement",
            "projectId": project_id,
            "rid": item.id,
            "order": position,
            "text": item.text,
            "type": item.type,
            "category": item.category,
            "priority": item.priority if item.priority in PRIORITIES else "standard",
            "anchor": item.anchor,
            "source_section": item.source_section,
            "keywords": item.keywords,
            "assignee": "",
            "assignee_name": "",
            "status": "extracted",
            "answer_version": 0,
            "answer": None,
            "rev": 1,
            "created_at": now,
            "updated_at": now,
            "updated_by": actor.oid,
        }
        for position, item in enumerate(items, start=1)
    ]


def create_project(
    store: LedgerStore,
    settings: Settings,
    user: UserClaims,
    *,
    name: str,
    requirements: list[dict[str, Any]],
    rfp_id: str | None = None,
    guidance: str = "",
    groups: list[str] | None = None,
) -> Doc:
    """Create a project with its requirements; returns the full state."""
    shared = resolve_share_groups(groups or [], user, settings)
    actor = Actor.of(user)
    project_id = uuid.uuid4().hex
    now = _now()

    docs = _requirement_docs(project_id, requirements, actor, now)
    if not docs:
        raise HTTPException(status_code=422, detail="A project needs at least one requirement")
    if len(docs) > settings.ledger_max_requirements:
        raise HTTPException(
            status_code=422,
            detail=f"A project holds at most {settings.ledger_max_requirements} requirements; this RFP has {len(docs)}.",
        )

    project: Doc = {
        "id": "project",
        "kind": "project",
        "projectId": project_id,
        "name": name.strip()[:MAX_NAME],
        "rfp_id": rfp_id or "",
        "owner": user.oid,
        "owner_name": actor.name,
        "groups": shared,
        "guidance": guidance,
        "requirement_count": len(docs),
        "created_at": now,
        "updated_at": now,
    }
    event = new_event(project_id, actor, "project.created", requirements=len(docs), rfp_id=rfp_id or "")
    first = [ledger_store.create(project), ledger_store.create(event)] + [
        ledger_store.create(d) for d in docs[: ledger_store.MAX_BATCH - 2]
    ]
    try:
        store.apply(project_id, first)
        rest = docs[ledger_store.MAX_BATCH - 2 :]
        for start in range(0, len(rest), ledger_store.MAX_BATCH):
            store.apply(project_id, [ledger_store.create(d) for d in rest[start : start + ledger_store.MAX_BATCH]])
    except Exception:
        # Never leave half a project behind
        try:
            store.delete_project(project_id)
        except Exception:
            logger.exception("Could not clean up the partly created project %s", project_id)
        raise
    logger.info("Project %s created by %s with %d requirements", project_id, user.oid, len(docs))
    return get_state(store, project_id, user, settings)


def list_projects(store: LedgerStore, user: UserClaims, settings: Settings) -> list[Doc]:
    projects = store.list_projects(owner=user.oid, groups=list(user.groups), everything=is_admin(user, settings))
    visible = [p for p in projects if can_view(p, user, settings)]  # the store's filter, checked again here
    visible.sort(key=lambda p: p.get("created_at", ""), reverse=True)
    return [_summary(p, user, settings) for p in visible]


def _out(doc: Doc, *, keep_id: bool = False) -> Doc:
    """A document as the API shows it: no storage bookkeeping, no partition key, no document kind.

    A document's own ``id`` is an internal key (``req:R1``) and is dropped unless it is meaningful (events).
    """
    hidden = {"kind", "projectId"} | (set() if keep_id else {"id"})
    return {k: v for k, v in public(doc).items() if k not in hidden}


def _summary(project: Doc, user: UserClaims, settings: Settings) -> Doc:
    summary = _out(project)
    summary["id"] = project["projectId"]
    summary.pop("guidance", None)
    summary["can_manage"] = can_manage(project.get("owner"), user, settings)
    return summary


def get_state(store: LedgerStore, project_id: str, user: UserClaims, settings: Settings) -> Doc:
    """The whole project as the UI needs it: the project and its requirements with their current answers."""
    project = load_project(store, project_id, user, settings)
    requirements = store.query(project_id, "requirement", order_by="order")
    counts = {status: 0 for status in STATUSES}
    for requirement in requirements:
        counts[requirement.get("status", "extracted")] = counts.get(requirement.get("status", "extracted"), 0) + 1
    return {
        "project": _summary(project, user, settings),
        "requirements": [_out(r) for r in requirements],
        "counts": counts,
    }


def update_project(
    store: LedgerStore,
    settings: Settings,
    user: UserClaims,
    project_id: str,
    *,
    name: str | None = None,
    groups: list[str] | None = None,
) -> Doc:
    project = load_project(store, project_id, user, settings)
    _require_manage(project, user, settings)
    changes: dict[str, Any] = {}
    if name is not None and name.strip()[:MAX_NAME] != project["name"]:
        changes["name"] = {"from": project["name"], "to": name.strip()[:MAX_NAME]}
        project["name"] = name.strip()[:MAX_NAME]
    if groups is not None:
        shared = resolve_share_groups(groups, user, settings)
        if shared != project.get("groups", []):
            changes["groups"] = {"from": project.get("groups", []), "to": shared}
            project["groups"] = shared
    if not changes:
        return _summary(project, user, settings)
    project["updated_at"] = _now()
    try:
        store.apply(
            project_id,
            [ledger_store.replace(project), ledger_store.create(new_event(project_id, Actor.of(user), "project.updated", **changes))],
        )
    except Conflict:
        raise _conflict() from None
    return _summary(store.get(project_id, "project") or project, user, settings)


def delete_project(store: LedgerStore, settings: Settings, user: UserClaims, project_id: str) -> int:
    project = load_project(store, project_id, user, settings)
    _require_manage(project, user, settings)
    removed = store.delete_project(project_id)
    logger.info("Project %s deleted by %s (%d documents)", project_id, user.oid, removed)
    return removed


# ── Requirements ─────────────────────────────────────────


def _load_requirement(store: LedgerStore, project_id: str, rid: str) -> Doc:
    requirement = store.get(project_id, req_doc_id(rid))
    if requirement is None:
        raise HTTPException(status_code=404, detail="Requirement not found")
    return requirement


def get_requirement(store: LedgerStore, project_id: str, rid: str, user: UserClaims, settings: Settings) -> Doc:
    load_project(store, project_id, user, settings)
    return _out(_load_requirement(store, project_id, rid))


def _check_rev(requirement: Doc, expected_rev: int | None) -> None:
    if expected_rev is not None and expected_rev != requirement["rev"]:
        raise _conflict()


def update_requirement(
    store: LedgerStore,
    settings: Settings,
    user: UserClaims,
    project_id: str,
    rid: str,
    changes: dict[str, Any],
    expected_rev: int | None = None,
) -> Doc:
    """Edit a requirement's text, category, priority, assignee or status. Attributed, timestamped, audited."""
    load_project(store, project_id, user, settings)
    requirement = _load_requirement(store, project_id, rid)
    _check_rev(requirement, expected_rev)

    recorded: dict[str, Any] = {}
    for field, value in changes.items():
        if requirement.get(field) != value:
            recorded[field] = {"from": requirement.get(field), "to": value}
    if not recorded:
        return _out(requirement)
    if recorded.get("status", {}).get("to") == "approved" and not requirement.get("answer_version"):
        raise HTTPException(status_code=409, detail="There is no answer to approve yet")

    for field, change in recorded.items():
        requirement[field] = change["to"]
    actor = Actor.of(user)
    requirement.update(rev=requirement["rev"] + 1, updated_at=_now(), updated_by=actor.oid)
    try:
        store.apply(
            project_id,
            [
                ledger_store.replace(requirement),
                ledger_store.create(new_event(project_id, actor, "requirement.updated", rid, changes=recorded)),
            ],
        )
    except Conflict:
        raise _conflict() from None
    return _out(_load_requirement(store, project_id, rid))


# ── Answers ──────────────────────────────────────────────


_EVIDENCE_KEYS = ("title", "source_url", "quote", "source_type", "authority", "effective_date", "version",
                  "approval_status", "section")
MAX_EVIDENCE = 20


def _evidence(value: Any) -> list[dict[str, Any]]:
    """An answer's evidence as the ledger keeps it: the citation plus where it came from (source type, date,
    version, section, page), so a reader can weigh it without going back to the knowledge base."""
    if not isinstance(value, list):
        return []
    kept: list[dict[str, Any]] = []
    for entry in value[:MAX_EVIDENCE]:
        if not isinstance(entry, dict):
            continue
        item: dict[str, Any] = {k: str(entry.get(k) or "")[:2000] for k in _EVIDENCE_KEYS[:3]}
        item.update({k: str(entry[k])[:200] for k in _EVIDENCE_KEYS[3:] if entry.get(k) not in (None, "")})
        if isinstance(entry.get("page"), int) and not isinstance(entry.get("page"), bool):
            item["page"] = entry["page"]
        kept.append(item)
    return kept


def add_answer(
    store: LedgerStore,
    settings: Settings,
    project_id: str,
    rid: str,
    content: dict[str, Any],
    *,
    actor: Actor,
    source: str,
    job_id: str = "",
    restored_from: int | None = None,
    expected_rev: int | None = None,
) -> Doc:
    """Record a new version of a requirement's answer (never overwriting an earlier one).

    A model answer makes the requirement ``drafted``; an answer a person writes makes it ``edited``. Either way an
    earlier approval is withdrawn: a changed answer needs approving again.
    """
    requirement = _load_requirement(store, project_id, rid)
    _check_rev(requirement, expected_rev)

    version = requirement["answer_version"] + 1
    now = _now()
    answer = normalize_answer(content, require_meets=False)
    document: Doc = {
        "id": _answer_doc_id(rid, version),
        "kind": "answer",
        "projectId": project_id,
        "rid": rid,
        "version": version,
        "answer_markdown": answer["answer_markdown"],
        "meets": answer.get("meets", ""),
        "evidence": _evidence(content.get("evidence")),
        "assumptions": answer["assumptions"],
        "risks": answer["risks"],
        "followups": answer["followups"],
        "kb_evidence_found": bool(content.get("kb_evidence_found", False)),
        "source": source,
        "author": actor.oid,
        "author_name": actor.name,
        "job_id": job_id,
        "created_at": now,
    }
    if restored_from is not None:
        document["restored_from"] = restored_from

    requirement.update(
        answer={k: v for k, v in document.items() if k not in ("id", "kind", "projectId", "rid")},
        answer_version=version,
        status="drafted" if source == "model" else "edited",
        rev=requirement["rev"] + 1,
        updated_at=now,
        updated_by=actor.oid,
    )
    event = new_event(
        project_id, actor, "answer.added", rid, version=version, source=source,
        **({"restored_from": restored_from} if restored_from is not None else {}),
        **({"job_id": job_id} if job_id else {}),
    )
    try:
        store.apply(
            project_id,
            [ledger_store.replace(requirement), ledger_store.create(document), ledger_store.create(event)],
        )
    except Conflict:
        raise _conflict() from None
    except ledger_store.AlreadyExists:
        raise _conflict() from None
    return _out(_load_requirement(store, project_id, rid))


def list_answers(store: LedgerStore, project_id: str, rid: str, user: UserClaims, settings: Settings) -> list[Doc]:
    """Every version of a requirement's answer, newest first."""
    load_project(store, project_id, user, settings)
    _load_requirement(store, project_id, rid)
    return [_out(a) for a in store.query(project_id, "answer", rid=rid, order_by="version", descending=True)]


def restore_answer(
    store: LedgerStore,
    settings: Settings,
    user: UserClaims,
    project_id: str,
    rid: str,
    version: int,
    expected_rev: int | None = None,
) -> Doc:
    """Make an earlier version current again, as a new version (history is never rewritten)."""
    load_project(store, project_id, user, settings)
    earlier = store.get(project_id, _answer_doc_id(rid, version))
    if earlier is None:
        raise HTTPException(status_code=404, detail="That answer version does not exist")
    return add_answer(
        store, settings, project_id, rid, earlier, actor=Actor.of(user), source="user", restored_from=version,
        expected_rev=expected_rev,
    )


# ── Audit trail ──────────────────────────────────────────


def list_events(
    store: LedgerStore,
    project_id: str,
    user: UserClaims,
    settings: Settings,
    *,
    rid: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[Doc]:
    """Who changed what and when, newest first."""
    load_project(store, project_id, user, settings)
    events = store.query(project_id, "event", rid=rid, order_by="at", descending=True, limit=limit, offset=offset)
    return [_out(e, keep_id=True) for e in events]
