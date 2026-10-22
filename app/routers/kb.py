"""The knowledge base: upload, list, re-ingest, delete and re-index documents."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated

from azure.core.exceptions import ResourceNotFoundError
from fastapi import APIRouter, Depends, File, Form, Query, UploadFile

from app import azure_clients
from app.auth.access import (
    KbAcl,
    can_manage,
    can_view_kb,
    kb_metadata,
    parse_group_list,
    parse_kb_acl,
    require_admin,
    resolve_share_groups,
)
from app.auth.entra import UserClaims, get_current_user
from app.auth.ratelimit import rate_limited
from app.config import Settings, get_settings
from app.ingestion import kb_ingest as ingestion
from app.ingestion.provenance import provenance_from_metadata
from app.schemas import IngestRequest
from app.services.kb_access import (
    checked_provenance,
    delete_kb_chunks,
    existing_kb_document,
    load_kb_document,
    require_kb_manage,
)
from app.services.uploads import read_upload, validated_upload_filename
from app.validation import KbBlobName, Segment

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/kb", tags=["knowledge-base"])


@router.post("/upload", dependencies=[Depends(rate_limited)])
def kb_upload(
    file: UploadFile = File(...),
    client_id: Annotated[Segment, Form()] = "demo-client",
    project_id: Annotated[Segment, Form()] = "demo-project",
    solution_area: Annotated[Segment, Form()] = "general",
    share_with_groups: Annotated[str, Form()] = "",
    source_type: Annotated[str | None, Form()] = None,
    effective_date: Annotated[str | None, Form()] = None,
    version: Annotated[str | None, Form()] = None,
    authority: Annotated[str | None, Form()] = None,
    owner_name: Annotated[str | None, Form()] = None,
    approval_status: Annotated[str | None, Form()] = None,
    user: UserClaims = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    """Upload a reference document, store in Blob, and ingest into the search index.

    The document is private to the uploader unless ``share_with_groups`` (comma-separated group IDs
    the uploader belongs to) is given. Uploading again replaces the document and its sharing; only
    the owner (or an administrator) can replace an existing document.

    ``source_type`` (``authoritative``, ``approved_answer``, ``historical``, ``external``), ``effective_date``
    (``YYYY-MM-DD``, default today), ``version``, ``authority``, ``owner_name`` and ``approval_status`` say how
    far the document should be trusted and drive ranking at retrieval. Only an administrator can mark a source
    ``authoritative``. A replaced document keeps the values it had for any field not given.
    """
    filename = validated_upload_filename(file, (".pdf", ".docx"))
    groups = resolve_share_groups(parse_group_list(share_with_groups), user, settings)

    blob_name = f"{client_id}/{project_id}/{solution_area}/{filename}"
    container = azure_clients.container(settings, settings.storage_kb_container)

    exists, existing_acl, existing_provenance = existing_kb_document(container, blob_name)
    if exists:
        require_kb_manage(existing_acl, blob_name, user, settings)
    owner = existing_acl.owner if existing_acl else user.oid  # an admin replacing a file keeps its owner
    provenance = checked_provenance(
        user,
        settings,
        source_type=source_type,
        effective_date=effective_date or "",  # a new upload is effective from today unless it says otherwise
        version=version,
        authority=authority,
        owner=owner_name if owner_name is not None else (existing_provenance.owner or user.name),
        approval_status=approval_status,
        fallback_date=datetime.now(UTC),
        base=existing_provenance if exists else None,
        content_changed=exists,
    )

    data = read_upload(file, settings)
    container.upload_blob(
        name=blob_name, data=data, overwrite=True, metadata=kb_metadata(owner, groups, provenance.to_metadata())
    )
    logger.info("Uploaded KB doc %s (%d bytes)", blob_name, len(data))

    count = ingestion.ingest_blob(
        blob_name=blob_name,
        client_id=client_id,
        project_id=project_id,
        solution_area=solution_area,
        oids=[owner],
        groups=groups,
        provenance=provenance,
    )

    return {
        "blob_name": blob_name,
        "filename": filename,
        "size_bytes": len(data),
        "indexed_chunks": count,
        "client_id": client_id,
        "project_id": project_id,
        "solution_area": solution_area,
        "shared_with_groups": groups,
        "provenance": provenance.to_dict(),
    }


@router.post("/ingest", dependencies=[Depends(rate_limited)])
def kb_ingest(
    req: IngestRequest,
    user: UserClaims = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    """Re-ingest a KB document from Blob → AI Search. Only its owner (or an administrator) can.

    Who can retrieve the indexed chunks comes from the blob's recorded owner and shared groups,
    never from the request. An administrator ingesting a document that has no recorded owner
    becomes its owner. Provenance fields in the request (for example a new ``source_type`` or
    ``effective_date``) are recorded on the document first; fields left out keep their current value.
    """
    container = azure_clients.container(settings, settings.storage_kb_container)
    acl = load_kb_document(container, req.blob_name, user, settings)
    require_kb_manage(acl, req.blob_name, user, settings)
    _, _, current = existing_kb_document(container, req.blob_name)
    provenance = checked_provenance(
        user, settings, base=current, fallback_date=datetime.now(UTC), **req.provenance_fields()
    )
    claiming = acl is None
    if claiming:
        acl = KbAcl(owner=user.oid)
    if claiming or provenance != current:
        container.get_blob_client(req.blob_name).set_blob_metadata(
            kb_metadata(acl.owner, acl.groups, provenance.to_metadata())
        )

    client_id, project_id, solution_area, _ = req.blob_name.split("/")
    count = ingestion.ingest_blob(
        blob_name=req.blob_name,
        client_id=client_id,
        project_id=project_id,
        solution_area=solution_area,
        oids=[acl.owner],
        groups=list(acl.groups),
        redaction_level=req.redaction_level,
        provenance=provenance,
    )
    return {"indexed_chunks": count, "blob_name": req.blob_name, "provenance": provenance.to_dict()}


@router.get("/list")
def kb_list(
    user: UserClaims = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    """List the knowledge-base documents you own or that are shared with one of your groups."""
    container = azure_clients.container(settings, settings.storage_kb_container)

    docs = []
    # A storage failure propagates (as a clear 503) instead of looking like an empty knowledge base.
    for blob in container.list_blobs(include=["metadata"]):
        acl = parse_kb_acl(blob.metadata)
        if not can_view_kb(acl, user, settings):
            continue
        parts = blob.name.split("/")
        provenance = provenance_from_metadata(blob.metadata, blob.last_modified)
        docs.append(
            {
                "blob_name": blob.name,
                "filename": parts[-1] if parts else blob.name,
                "client_id": parts[0] if len(parts) > 1 else "",
                "project_id": parts[1] if len(parts) > 2 else "",
                "solution_area": parts[2] if len(parts) > 3 else "",
                "size_bytes": blob.size,
                "last_modified": blob.last_modified.isoformat() if blob.last_modified else "",
                "owned_by_me": acl is not None and acl.owner == user.oid,
                "can_delete": can_manage(acl.owner if acl else None, user, settings),
                "shared_with_groups": list(acl.groups) if acl else [],
                "provenance": provenance.to_dict(),
            }
        )

    return {"documents": docs, "count": len(docs)}


@router.delete("/delete")
def kb_delete(
    blob_name: Annotated[KbBlobName, Query()],
    user: UserClaims = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    """Delete a KB document from Blob Storage and remove its chunks from the search index.

    Only the document's owner (or an administrator) can delete it.
    """
    container = azure_clients.container(settings, settings.storage_kb_container)
    require_kb_manage(load_kb_document(container, blob_name, user, settings), blob_name, user, settings)

    try:
        container.delete_blob(blob_name)
        logger.info("Deleted blob: %s", blob_name)
    except ResourceNotFoundError:
        logger.info("Blob %s was already gone", blob_name)

    return {"blob_name": blob_name, "deleted_chunks": delete_kb_chunks(settings, blob_name)}


@router.post("/reindex", dependencies=[Depends(rate_limited)])
def kb_reindex(
    user: UserClaims = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    """Re-index every KB document (administrators only).

    Each document keeps the access recorded on its blob (owner and shared groups). Documents with no
    recorded owner are skipped: ingest one explicitly with POST /kb/ingest to claim it.
    """
    require_admin(user, settings)
    container = azure_clients.container(settings, settings.storage_kb_container)

    results = []
    for blob in container.list_blobs(include=["metadata"]):
        acl = parse_kb_acl(blob.metadata)
        parts = blob.name.split("/")
        if acl is None or len(parts) != 4:
            reason = "no recorded owner" if acl is None else "unexpected blob name"
            results.append({"blob_name": blob.name, "chunks": 0, "status": f"skipped: {reason}"})
            continue
        try:
            count = ingestion.ingest_blob(
                blob_name=blob.name,
                client_id=parts[0],
                project_id=parts[1],
                solution_area=parts[2],
                oids=[acl.owner],
                groups=list(acl.groups),
                provenance=provenance_from_metadata(blob.metadata, blob.last_modified),
            )
            results.append({"blob_name": blob.name, "chunks": count, "status": "ok"})
        except Exception as exc:
            results.append({"blob_name": blob.name, "chunks": 0, "status": f"error: {exc}"})

    total_chunks = sum(r["chunks"] for r in results)
    logger.info("Re-indexed %d documents, %d total chunks", len(results), total_chunks)
    return {"documents": len(results), "total_chunks": total_chunks, "details": results}
