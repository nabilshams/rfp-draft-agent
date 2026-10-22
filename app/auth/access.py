"""Authorisation rules for RFPs, knowledge-base documents and admin-only operations.

Ownership is recorded in blob metadata:

* RFP uploads – ``owner_oid``
* KB documents – ``owner_oid`` and ``shared_groups`` (comma-separated group IDs)

Rules:

* An RFP belongs to whoever uploaded it. Only the owner (or an admin) can extract, draft from,
  or download its outputs.
* A KB document belongs to whoever uploaded it and is private by default. The owner can share it
  with groups they belong to; members of those groups can retrieve and list it. Only the owner
  (or an admin) can re-ingest or delete it.
* Documents with no recorded owner (uploaded before ownership was recorded) are admin-only.
* Admins are the object IDs listed in ``ADMIN_OIDS``. With ``AUTH_MODE=disabled`` there is no real
  identity, so everyone is treated as an admin.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, status

from app.auth.entra import UserClaims
from app.config import Settings

logger = logging.getLogger(__name__)

OWNER_KEY = "owner_oid"
SHARED_GROUPS_KEY = "shared_groups"

_GROUP_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def is_admin(user: UserClaims, settings: Settings) -> bool:
    return settings.auth_mode == "disabled" or user.oid in settings.admin_oid_set


def require_admin(user: UserClaims, settings: Settings) -> None:
    if not is_admin(user, settings):
        logger.warning("Admin-only operation denied for user %s", user.oid)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Administrator access is required")


def can_manage(owner: str | None, user: UserClaims, settings: Settings) -> bool:
    """Owner or admin. A missing owner (legacy document) is admin-only."""
    return is_admin(user, settings) or (bool(owner) and owner == user.oid)


# ── Knowledge-base documents ─────────────────────────────


@dataclass(frozen=True)
class KbAcl:
    owner: str
    groups: tuple[str, ...] = ()


def kb_metadata(owner_oid: str, groups: Iterable[str], extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Blob metadata that records a KB document's owner and the groups it is shared with (plus *extra* entries)."""
    return {OWNER_KEY: owner_oid, SHARED_GROUPS_KEY: ",".join(groups), **(extra or {})}


def parse_kb_acl(metadata: Mapping[str, str | None] | None) -> KbAcl | None:
    """The recorded ACL, or ``None`` for documents that have no owner.

    Azure returns a metadata value that was stored empty (a document shared with no group) as ``None`` when
    listing blobs, so every value is treated as possibly missing.
    """
    owner = (metadata or {}).get(OWNER_KEY) or ""
    if not owner:
        return None
    raw_groups = (metadata or {}).get(SHARED_GROUPS_KEY) or ""
    return KbAcl(owner=owner, groups=tuple(g for g in raw_groups.split(",") if g))


def can_view_kb(acl: KbAcl | None, user: UserClaims, settings: Settings) -> bool:
    if is_admin(user, settings):
        return True
    if acl is None:
        return False
    return acl.owner == user.oid or bool(set(acl.groups) & set(user.groups))


def parse_group_list(text: str) -> list[str]:
    """Split a comma-separated list of group IDs, dropping blanks and duplicates."""
    groups: list[str] = []
    for item in text.split(","):
        item = item.strip()
        if item and item not in groups:
            groups.append(item)
    return groups


def resolve_share_groups(requested: Iterable[str], user: UserClaims, settings: Settings) -> list[str]:
    """Validate the groups a document is to be shared with.

    Callers can only share with groups they belong to; admins can name any group.
    """
    groups = list(dict.fromkeys(requested))
    for group in groups:
        if not _GROUP_RE.fullmatch(group):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid group ID: {group!r}")
    outside = [g for g in groups if g not in user.groups]
    if outside and not is_admin(user, settings):
        logger.warning("User %s tried to share with groups they are not in: %s", user.oid, outside)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only share documents with groups you belong to",
        )
    return groups


# ── RFPs ─────────────────────────────────────────────────


def require_rfp_access(container: Any, rfp_id: str, user: UserClaims, settings: Settings) -> Any:
    """Return the RFP's blob if the user may use it; otherwise raise 404.

    A document the user may not access is reported as not found so its existence isn't revealed.
    """
    blob = next(iter(container.list_blobs(name_starts_with=f"{rfp_id}/", include=["metadata"])), None)
    if blob is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"No RFP found for id {rfp_id}")
    if not can_manage((blob.metadata or {}).get(OWNER_KEY), user, settings):
        logger.warning("User %s denied access to RFP %s", user.oid, rfp_id)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"No RFP found for id {rfp_id}")
    return blob
