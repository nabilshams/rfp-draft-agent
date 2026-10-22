"""Draft jobs: long drafts run in the background and clients poll for progress.

Azure Container Apps cuts a request off after 240 seconds, so a draft can't be tied to one HTTP request.
``submit`` stores a job record and starts the draft on a worker thread; the worker records every event
(the same events ``/draft/stream`` sends) in the job's blob, and a heartbeat records that the job is alive
from the moment it is submitted (a queued job waits for a free worker); ``get_job`` reads it back.

* **State lives in Blob Storage** (one JSON blob per job), not in process memory, so any replica can answer
  a poll and the record survives a restart.
* **A restart loses the work in flight.** The job stays ``running`` with an old heartbeat until somebody asks
  for it; ``get_job`` then reports it as failed ("interrupted"), and the user starts it again.
* **Cancelling** writes a marker blob that the worker checks between events.
* **Access** follows the RFP model: the job's owner or an administrator, otherwise 404.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from azure.core.exceptions import ResourceNotFoundError
from fastapi import HTTPException

from app import azure_clients
from app.auth.access import can_manage
from app.auth.entra import UserClaims
from app.config import Settings
from app.schemas import DraftRequest
from app.services.draft_pipeline import DraftSink, stream_draft

logger = logging.getLogger(__name__)

ACTIVE_STATES = ("queued", "running")
TERMINAL_STATES = ("succeeded", "failed", "cancelled")

GENERIC_FAILURE = "Draft generation failed. Check the server logs for details."
LOST_JOB = "The draft was interrupted (the server restarted or lost contact with it). Please start it again."


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _age_seconds(timestamp: str) -> float:
    try:
        return (datetime.now(UTC) - datetime.fromisoformat(timestamp)).total_seconds()
    except ValueError:
        return float("inf")


@dataclass
class Job:
    id: str
    owner_oid: str
    rfp_id: str
    approach: str
    status: str = "queued"
    total: int = 0
    processed: int = 0
    events: list[dict[str, Any]] = field(default_factory=list)
    outputs: dict[str, str] | None = None
    error: str = ""
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    heartbeat_at: str = field(default_factory=_now)
    # Set when the draft belongs to a ledger project (its answers are written to the ledger as they are produced)
    project_id: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Job:
        known = cls.__dataclass_fields__.keys()
        return cls(**{k: v for k, v in data.items() if k in known})


# ── Storage ──────────────────────────────────────────────

_ready_containers: set[str] = set()


class JobStore:
    """One JSON blob per job, plus an empty ``<id>.cancel`` blob when cancellation was requested."""

    def __init__(self, settings: Settings):
        self._settings = settings

    def _container(self):
        name = self._settings.storage_jobs_container
        container = azure_clients.container(self._settings, name)
        if name not in _ready_containers:
            try:
                container.create_container()
            except Exception:
                pass  # already exists
            _ready_containers.add(name)
        return container

    def save(self, job: Job) -> None:
        self._container().upload_blob(f"{job.id}.json", json.dumps(asdict(job)), overwrite=True)

    def load(self, job_id: str) -> Job | None:
        try:
            data = self._container().download_blob(f"{job_id}.json").readall()
        except ResourceNotFoundError:
            return None
        return Job.from_dict(json.loads(data))

    def request_cancel(self, job_id: str) -> None:
        self._container().upload_blob(f"{job_id}.cancel", b"", overwrite=True)

    def cancel_requested(self, job_id: str) -> bool:
        try:
            self._container().get_blob_client(f"{job_id}.cancel").get_blob_properties()
        except ResourceNotFoundError:
            return False
        return True


# ── A job running in this process ────────────────────────


class LiveJob:
    """A job plus what keeps its record current: a lock around writes and a heartbeat thread."""

    def __init__(self, settings: Settings, job: Job):
        self.job = job
        self._settings = settings
        self._store = JobStore(settings)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._beat = threading.Thread(target=self._heartbeat, name=f"job-heartbeat-{job.id[:8]}", daemon=True)

    def update(self, mutate: Callable[[Job], None] | None = None, **changes: Any) -> None:
        """Change the job and write it; the lock keeps the worker and the heartbeat from interleaving."""
        with self._lock:
            if mutate:
                mutate(self.job)
            for key, value in changes.items():
                setattr(self.job, key, value)
            self.job.updated_at = self.job.heartbeat_at = _now()
            self._store.save(self.job)

    def start_heartbeat(self) -> None:
        self._beat.start()

    def stop_heartbeat(self) -> None:
        self._stop.set()
        if self._beat.is_alive():
            self._beat.join(timeout=5)

    def _heartbeat(self) -> None:
        while not self._stop.wait(self._settings.job_heartbeat_seconds):
            try:
                self.update()
            except Exception as exc:
                logger.warning("Could not record the heartbeat of job %s: %s", self.job.id, exc)


_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()
_active: dict[str, LiveJob] = {}  # jobs submitted in this process and not finished
_active_lock = threading.Lock()


def _get_executor(settings: Settings) -> ThreadPoolExecutor:
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=settings.job_workers, thread_name_prefix="draft-job")
        return _executor


def reset() -> None:
    """Forget the worker pool and in-process state (tests)."""
    global _executor
    with _executor_lock:
        if _executor is not None:
            _executor.shutdown(wait=True)
        _executor = None
    with _active_lock:
        for live in _active.values():
            live.stop_heartbeat()
        _active.clear()
    _ready_containers.clear()


def shutdown() -> None:
    """Stop taking jobs at application shutdown. Jobs still running are reported as interrupted when asked for."""
    with _executor_lock:
        if _executor is not None:
            _executor.shutdown(wait=False, cancel_futures=True)


def _failure_message(exc: BaseException) -> str:
    return exc.detail if isinstance(exc, HTTPException) and isinstance(exc.detail, str) else GENERIC_FAILURE


def _record(event: dict[str, Any]) -> Callable[[Job], None]:
    def apply(job: Job) -> None:
        job.events.append(event)
        if event.get("type") == "response":
            job.processed += 1
        elif event.get("type") == "complete":
            job.outputs = event.get("outputs")

    return apply


def _run_job(
    settings: Settings,
    live: LiveJob,
    req: DraftRequest,
    requirements: list[dict[str, Any]],
    user: UserClaims,
    allow_web_search: bool,
    guidance: str = "",
    sink: DraftSink | None = None,
) -> None:
    job = live.job
    store = JobStore(settings)
    events = None
    try:
        if store.cancel_requested(job.id):
            live.update(status="cancelled")
            return
        live.update(status="running")

        events = stream_draft(settings, req, requirements, user, allow_web_search, guidance, job.id, sink=sink)
        for event in events:
            live.update(_record(event))
            if store.cancel_requested(job.id):
                live.update(status="cancelled")
                return
        live.update(status="succeeded")
    except Exception as exc:
        logger.exception("Draft job %s failed", job.id)
        try:
            live.update(status="failed", error=_failure_message(exc))
        except Exception:
            logger.error("Could not record the failure of job %s", job.id)
    finally:
        if events is not None:
            events.close()
        _finish(live)


def _finish(live: LiveJob) -> None:
    live.stop_heartbeat()
    with _active_lock:
        _active.pop(live.job.id, None)


def submit(
    settings: Settings,
    user: UserClaims,
    req: DraftRequest,
    requirements: list[dict[str, Any]],
    allow_web_search: bool,
    guidance: str = "",
    *,
    sink: DraftSink | None = None,
    project_id: str = "",
    job_id: str | None = None,
) -> Job:
    """Store a new job and start it. Returns it in the ``queued`` state.

    With a *sink* (a project draft) each answer is handed to it as it is produced, so nothing is lost if the job is
    interrupted.
    """
    job = Job(
        id=job_id or uuid.uuid4().hex,
        owner_oid=user.oid,
        rfp_id=req.rfp_id,
        approach=req.approach,
        total=len(requirements),
        project_id=project_id,
    )
    live = LiveJob(settings, job)

    with _active_lock:
        running = sum(1 for other in _active.values() if other.job.owner_oid == user.oid)
        if running >= settings.job_max_active_per_user:
            raise HTTPException(
                status_code=429,
                detail=f"You already have {running} drafts in progress; wait for one to finish or cancel it.",
            )
        _active[job.id] = live

    try:
        JobStore(settings).save(job)
        live.start_heartbeat()
        _get_executor(settings).submit(
            _run_job, settings, live, req, requirements, user, allow_web_search, guidance, sink
        )
    except Exception:
        _finish(live)
        raise
    return job


# ── Reading jobs ─────────────────────────────────────────


def reconcile(settings: Settings, job: Job) -> Job:
    """The job as it really is: an unfinished job nobody is working on (a restart, a lost worker) becomes ``failed``."""
    with _active_lock:
        running_here = job.id in _active
    silent_for = _age_seconds(job.heartbeat_at)
    if job.status in ACTIVE_STATES and not running_here and silent_for > settings.job_stale_seconds:
        logger.warning("Job %s has had no heartbeat for %.0fs; marking it failed", job.id, silent_for)
        job.status, job.error, job.updated_at = "failed", LOST_JOB, _now()
        JobStore(settings).save(job)
    return job


def find_job(settings: Settings, job_id: str) -> Job | None:
    """A job by id with no access check (callers check access to whatever the job belongs to), reconciled."""
    job = JobStore(settings).load(job_id)
    return reconcile(settings, job) if job else None


def get_job(settings: Settings, user: UserClaims, job_id: str) -> Job:
    """A job the user may see (404 otherwise). An unfinished job nobody is working on is reported as failed."""
    job = JobStore(settings).load(job_id)
    if job is None or not can_manage(job.owner_oid, user, settings):
        if job is not None:
            logger.warning("User %s denied access to job %s", user.oid, job_id)
        raise HTTPException(status_code=404, detail="Draft job not found")
    return reconcile(settings, job)


def request_cancel(settings: Settings, job: Job) -> None:
    """Ask a running job to stop (no access check: the caller has decided who may)."""
    if job.status in ACTIVE_STATES:
        JobStore(settings).request_cancel(job.id)


def cancel_job(settings: Settings, user: UserClaims, job_id: str) -> Job:
    """Ask a job to stop. It stops between steps, so it may take a moment (or a whole agent call) to do so."""
    job = get_job(settings, user, job_id)
    if job.status in ACTIVE_STATES:
        JobStore(settings).request_cancel(job.id)
    return job
