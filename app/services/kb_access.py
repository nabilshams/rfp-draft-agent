"""Looking up knowledge-base documents on behalf of a user, and removing their search chunks."""

from __future__ import annotations

import hashlib
import logging

from azure.core.exceptions import ResourceNotFoundError
from fastapi import HTTPException

from app import azure_clients
from app.agents.retrieval import escape_odata_literal
from app.auth.access import KbAcl, can_manage, can_view_kb, parse_kb_acl
from app.auth.entra import UserClaims
from app.config import Settings

logger = logging.getLogger(__name__)

# Chunk IDs are derived from the blob name and chunk number; documents have at most this many chunks.
_MAX_CHUNKS_PER_DOCUMENT = 200


def existing_kb_acl(container, blob_name: str) -> tuple[bool, KbAcl | None]:
    """(exists, recorded ACL) for a KB blob."""
    try:
        props = container.get_blob_client(blob_name).get_blob_properties()
    except ResourceNotFoundError:
        return False, None
    return True, parse_kb_acl(props.metadata)


def load_kb_document(container, blob_name: str, user: UserClaims, settings: Settings) -> KbAcl | None:
    """The ACL of a KB document the user is allowed to see.

    Missing documents and documents the user cannot see both give 404 so existence isn't revealed.
    """
    exists, acl = existing_kb_acl(container, blob_name)
    if not exists or not can_view_kb(acl, user, settings):
        raise HTTPException(status_code=404, detail="Document not found")
    return acl


def require_kb_manage(acl: KbAcl | None, blob_name: str, user: UserClaims, settings: Settings) -> None:
    if not can_manage(acl.owner if acl else None, user, settings):
        logger.warning("User %s denied changes to KB document %s", user.oid, blob_name)
        raise HTTPException(status_code=403, detail="Only the document's owner or an administrator can do this")


def delete_kb_chunks(settings: Settings, blob_name: str) -> int:
    """Remove every search-index chunk that came from a KB blob; returns how many were removed."""
    search_client = azure_clients.search_client(settings)
    try:
        source_url = f"{settings.azure_storage_account_url}/{settings.storage_kb_container}/{blob_name}"
        results = search_client.search(
            search_text="*",
            filter=f"source_url eq '{escape_odata_literal(source_url)}'",
            select=["id"],
            top=1000,
        )
        doc_ids = [{"id": r["id"]} for r in results]
        if doc_ids:
            search_client.delete_documents(documents=doc_ids)
            logger.info("Deleted %d chunks for %s", len(doc_ids), blob_name)
        return len(doc_ids)
    except Exception as exc:
        logger.warning("Filter-based delete failed (%s), trying hash-based", exc)

    # Fallback: the chunk IDs are predictable
    try:
        doc_ids = [
            {"id": hashlib.md5(f"{blob_name}:{i}".encode(), usedforsecurity=False).hexdigest()}
            for i in range(_MAX_CHUNKS_PER_DOCUMENT)
        ]
        search_client.delete_documents(documents=doc_ids)
        return len(doc_ids)
    except Exception as exc:
        logger.error("Could not delete search chunks for %s: %s", blob_name, exc)
        return 0
