"""Where the ledger's documents are kept: a small interface with a Cosmos DB and an in-memory implementation.

Every document of a project (the project itself, its requirements, answer versions and audit events) has the same
``projectId`` and so lives in one Cosmos partition. That makes :meth:`LedgerStore.apply` atomic: a change to a
requirement, the new answer version and the audit event are written together or not at all.

Documents are plain dicts. ``kind`` is ``project``, ``requirement``, ``answer`` or ``event``; ``_etag`` is the
version a writer must present to replace or delete a document (optimistic concurrency).
"""

from __future__ import annotations

import copy
import itertools
import logging
import threading
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger(__name__)

Doc = dict[str, Any]

MAX_BATCH = 100  # Cosmos DB transactional batches hold at most 100 operations
_ORDER_FIELDS = frozenset({"order", "at", "version", "created_at"})


class LedgerError(Exception):
    """Base of the errors a store raises."""


class NotFound(LedgerError):
    pass


class AlreadyExists(LedgerError):
    pass


class Conflict(LedgerError):
    """The document changed since it was read (its etag no longer matches)."""


@dataclass(frozen=True)
class Op:
    """One write in an atomic batch. ``etag`` is required to replace or delete an existing document."""

    action: str  # "create", "replace", "upsert" or "delete"
    doc: Doc
    etag: str | None = None


def create(doc: Doc) -> Op:
    return Op("create", doc)


def replace(doc: Doc) -> Op:
    """Replace a document that was read earlier; the etag it carries must still be current."""
    return Op("replace", doc, doc.get("_etag"))


def delete(doc: Doc) -> Op:
    return Op("delete", doc, doc.get("_etag"))


class LedgerStore(Protocol):
    def get(self, project_id: str, doc_id: str) -> Doc | None: ...

    def query(
        self,
        project_id: str,
        kind: str,
        *,
        rid: str | None = None,
        order_by: str | None = None,
        descending: bool = False,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[Doc]: ...

    def list_projects(self, *, owner: str, groups: list[str], everything: bool) -> list[Doc]: ...

    def apply(self, project_id: str, ops: list[Op]) -> None: ...

    def delete_project(self, project_id: str) -> int: ...

    def ping(self) -> None: ...


def public(doc: Doc) -> Doc:
    """The document without storage bookkeeping (``_etag``, ``_ts``, ...)."""
    return {k: v for k, v in doc.items() if not k.startswith("_")}


def _check_order(order_by: str | None) -> None:
    if order_by is not None and order_by not in _ORDER_FIELDS:
        raise ValueError(f"cannot order by {order_by}")


# ── In memory ────────────────────────────────────────────


class MemoryStore:
    """A process-local store with the same rules as Cosmos (etags, atomic batches). For local development and tests."""

    def __init__(self) -> None:
        self._docs: dict[tuple[str, str], Doc] = {}
        self._versions = itertools.count(1)
        self._lock = threading.RLock()

    def _stamp(self, doc: Doc) -> Doc:
        stored = copy.deepcopy(doc)
        stored["_etag"] = f'"{next(self._versions)}"'
        return stored

    def get(self, project_id: str, doc_id: str) -> Doc | None:
        with self._lock:
            doc = self._docs.get((project_id, doc_id))
            return copy.deepcopy(doc) if doc else None

    def query(
        self,
        project_id: str,
        kind: str,
        *,
        rid: str | None = None,
        order_by: str | None = None,
        descending: bool = False,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[Doc]:
        _check_order(order_by)
        with self._lock:
            found = [
                copy.deepcopy(d)
                for (pid, _), d in self._docs.items()
                if pid == project_id and d.get("kind") == kind and (rid is None or d.get("rid") == rid)
            ]
        if order_by:
            found.sort(key=lambda d: d.get(order_by, 0), reverse=descending)
        found = found[offset:]
        return found[:limit] if limit is not None else found

    def list_projects(self, *, owner: str, groups: list[str], everything: bool) -> list[Doc]:
        with self._lock:
            projects = [copy.deepcopy(d) for d in self._docs.values() if d.get("kind") == "project"]
        if not everything:
            projects = [p for p in projects if p.get("owner") == owner or set(p.get("groups", [])) & set(groups)]
        return projects

    def apply(self, project_id: str, ops: list[Op]) -> None:
        if len(ops) > MAX_BATCH:
            raise ValueError(f"a batch holds at most {MAX_BATCH} operations")
        with self._lock:
            staged = dict(self._docs)
            for op in ops:
                key = (project_id, op.doc["id"])
                if op.doc.get("projectId", project_id) != project_id:
                    raise ValueError("every document of a batch must belong to the same project")
                current = staged.get(key)
                if op.action == "create":
                    if current is not None:
                        raise AlreadyExists(op.doc["id"])
                    staged[key] = self._stamp(op.doc)
                elif op.action == "upsert":
                    staged[key] = self._stamp(op.doc)
                elif op.action in ("replace", "delete"):
                    if current is None:
                        raise NotFound(op.doc["id"])
                    if op.etag is not None and op.etag != current["_etag"]:
                        raise Conflict(op.doc["id"])
                    if op.action == "delete":
                        del staged[key]
                    else:
                        staged[key] = self._stamp(op.doc)
                else:
                    raise ValueError(f"unknown action {op.action}")
            self._docs = staged  # all or nothing

    def delete_project(self, project_id: str) -> int:
        with self._lock:
            keys = [k for k in self._docs if k[0] == project_id]
            for key in keys:
                del self._docs[key]
            return len(keys)

    def ping(self) -> None:
        return None


# ── Cosmos DB ────────────────────────────────────────────


class CosmosStore:
    """The ledger in an Azure Cosmos DB for NoSQL container partitioned by ``/projectId``."""

    def __init__(self, container: Any) -> None:
        self._container = container

    def get(self, project_id: str, doc_id: str) -> Doc | None:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        try:
            return self._clean(self._container.read_item(item=doc_id, partition_key=project_id))
        except CosmosResourceNotFoundError:
            return None

    def query(
        self,
        project_id: str,
        kind: str,
        *,
        rid: str | None = None,
        order_by: str | None = None,
        descending: bool = False,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[Doc]:
        _check_order(order_by)
        sql = "SELECT * FROM c WHERE c.projectId = @project AND c.kind = @kind"
        parameters: list[dict[str, Any]] = [
            {"name": "@project", "value": project_id},
            {"name": "@kind", "value": kind},
        ]
        if rid is not None:
            sql += " AND c.rid = @rid"
            parameters.append({"name": "@rid", "value": rid})
        if order_by:
            sql += f" ORDER BY c.{order_by} {'DESC' if descending else 'ASC'}"
        if limit is not None or offset:
            sql += " OFFSET @offset LIMIT @limit"
            parameters += [
                {"name": "@offset", "value": offset},
                {"name": "@limit", "value": limit if limit is not None else 100000},
            ]
        items = self._container.query_items(query=sql, parameters=parameters, partition_key=project_id)
        return [self._clean(item) for item in items]

    def list_projects(self, *, owner: str, groups: list[str], everything: bool) -> list[Doc]:
        sql = "SELECT * FROM c WHERE c.kind = 'project'"
        parameters: list[dict[str, Any]] = []
        if not everything:
            sql += (
                " AND (c.owner = @owner OR EXISTS(SELECT VALUE g FROM g IN c.groups WHERE ARRAY_CONTAINS(@groups, g)))"
            )
            parameters = [{"name": "@owner", "value": owner}, {"name": "@groups", "value": list(groups)}]
        items = self._container.query_items(query=sql, parameters=parameters, enable_cross_partition_query=True)
        return [self._clean(item) for item in items]

    def apply(self, project_id: str, ops: list[Op]) -> None:
        from azure.cosmos.exceptions import CosmosBatchOperationError

        if len(ops) > MAX_BATCH:
            raise ValueError(f"a batch holds at most {MAX_BATCH} operations")
        if not ops:
            return
        batch: list[Any] = []
        for op in ops:
            doc = {k: v for k, v in op.doc.items() if not k.startswith("_")}
            doc.setdefault("projectId", project_id)
            options = {"if_match_etag": op.etag} if op.etag else {}
            if op.action == "create":
                batch.append(("create", (doc,)))
            elif op.action == "upsert":
                batch.append(("upsert", (doc,)))
            elif op.action == "replace":
                batch.append(("replace", (doc["id"], doc), options))
            elif op.action == "delete":
                batch.append(("delete", (doc["id"],), options))
            else:
                raise ValueError(f"unknown action {op.action}")
        try:
            self._container.execute_item_batch(batch_operations=batch, partition_key=project_id)
        except CosmosBatchOperationError as exc:
            status = getattr(exc, "status_code", None)
            index = getattr(exc, "error_index", None)
            name = ops[index].doc["id"] if isinstance(index, int) and 0 <= index < len(ops) else "?"
            if status == 409:
                raise AlreadyExists(name) from exc
            if status == 412:
                raise Conflict(name) from exc
            if status == 404:
                raise NotFound(name) from exc
            raise

    def delete_project(self, project_id: str) -> int:
        """Remove every document of the project; returns how many."""
        ids = [
            item["id"]
            for item in self._container.query_items(
                query="SELECT c.id FROM c WHERE c.projectId = @project",
                parameters=[{"name": "@project", "value": project_id}],
                partition_key=project_id,
            )
        ]
        for start in range(0, len(ids), MAX_BATCH):
            self._container.execute_item_batch(
                batch_operations=[("delete", (doc_id,)) for doc_id in ids[start : start + MAX_BATCH]],
                partition_key=project_id,
            )
        return len(ids)

    def ping(self) -> None:
        self._container.read()

    @staticmethod
    def _clean(item: Doc) -> Doc:
        """The document with only the system field the app uses (``_etag``)."""
        return {k: v for k, v in item.items() if not k.startswith("_") or k == "_etag"}


# ── Choosing the store ───────────────────────────────────

_memory = MemoryStore()


def reset_memory() -> None:
    global _memory
    _memory = MemoryStore()


def store_for(settings: Any) -> LedgerStore | None:
    """The configured store, or ``None`` when the ledger is off.

    ``LEDGER_BACKEND`` picks one; left empty, Cosmos DB is used when ``COSMOS_ENDPOINT`` is set.
    """
    from app import azure_clients

    backend = settings.ledger_backend or ("cosmos" if settings.cosmos_endpoint else "")
    if backend == "memory":
        return _memory
    if backend == "cosmos":
        return CosmosStore(azure_clients.cosmos_container(settings))
    return None
