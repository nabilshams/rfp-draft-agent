"""What kind of source a knowledge-base document is, and how much weight it deserves.

A current security policy and a three-year-old proposal answer are both "documents" to a search index, but they
are not equally trustworthy. Each document therefore carries *provenance* alongside its access rights:

* ``source_type``: ``authoritative`` (current policy, certification, product fact), ``approved_answer`` (a
  reviewed answer from a past response), ``historical`` (old proposals, unreviewed material) or ``external``.
* ``effective_date``: when the content became valid (defaults to when the file was last changed).
* ``version``, ``authority`` (who vouches for it), ``owner`` (who maintains it) and ``approval_status``.

Provenance is stored on the blob (like the access rights, so it survives re-indexing), copied onto every
chunk in the search index, and used at retrieval to rank current, authoritative material above old answers.
Nothing is hidden by ranking: a lower tier is only ordered later.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import quote, unquote

SOURCE_TYPES = ("authoritative", "approved_answer", "historical", "external")
APPROVAL_STATUSES = ("approved", "unreviewed", "draft", "superseded")
DEFAULT_SOURCE_TYPE = "historical"
DEFAULT_APPROVAL_STATUS = "unreviewed"

MAX_TEXT = 100

# Blob metadata keys. Values are percent-encoded because metadata must be plain ASCII.
_PREFIX = "kb_"
_FIELDS = ("source_type", "effective_date", "version", "authority", "owner", "approval_status")


class ProvenanceError(ValueError):
    """A provenance value the user supplied is not acceptable (reported as a 400)."""


@dataclass(frozen=True)
class Provenance:
    source_type: str = DEFAULT_SOURCE_TYPE
    effective_date: str = ""  # ISO date (YYYY-MM-DD)
    version: str = ""
    authority: str = ""
    owner: str = ""
    approval_status: str = DEFAULT_APPROVAL_STATUS

    def to_metadata(self) -> dict[str, str]:
        """Blob metadata for this provenance (empty values are left out)."""
        return {f"{_PREFIX}{name}": quote(getattr(self, name), safe="") for name in _FIELDS if getattr(self, name)}

    def to_dict(self) -> dict[str, str]:
        return {name: getattr(self, name) for name in _FIELDS}

    def to_index_fields(self) -> dict[str, object]:
        """The fields stored on every search chunk of the document."""
        return {
            "source_type": self.source_type,
            "authority": self.authority,
            "effective_date": index_date(self.effective_date),
            "version": self.version,
            "owner": self.owner,
            "approval_status": self.approval_status,
            # The old "last reviewed" field (an index field can't be dropped in place) now holds the real date.
            "last_reviewed_date": index_date(self.effective_date),
        }


def index_date(value: str) -> str | None:
    """An ISO date as the timestamp Azure AI Search stores (``None`` when there is none)."""
    return f"{value}T00:00:00Z" if value else None


def _clean_text(value: str | None, label: str) -> str:
    text = (value or "").strip()
    if len(text) > MAX_TEXT:
        raise ProvenanceError(f"{label} is longer than {MAX_TEXT} characters")
    if any(ord(c) < 32 for c in text):
        raise ProvenanceError(f"{label} contains control characters")
    return text


def _clean_date(value: str | None) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        raise ProvenanceError("effective_date must look like 2025-03-31")
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        raise ProvenanceError("effective_date is not a real date") from None
    if parsed > datetime.now(UTC).date():
        raise ProvenanceError("effective_date can't be in the future")
    return text


def build_provenance(
    *,
    source_type: str | None = None,
    effective_date: str | None = None,
    version: str | None = None,
    authority: str | None = None,
    owner: str | None = None,
    approval_status: str | None = None,
    fallback_date: datetime | None = None,
    base: Provenance | None = None,
) -> Provenance:
    """Validated provenance from user input. Fields left out (``None``) keep the value from *base*.

    With no date anywhere, *fallback_date* (when the file was last changed) is used, so the date is always real.
    """
    base = base or Provenance()
    chosen_type = (source_type or base.source_type).strip().lower()
    if chosen_type not in SOURCE_TYPES:
        raise ProvenanceError(f"source_type must be one of: {', '.join(SOURCE_TYPES)}")
    chosen_status = (approval_status or base.approval_status).strip().lower()
    if chosen_status not in APPROVAL_STATUSES:
        raise ProvenanceError(f"approval_status must be one of: {', '.join(APPROVAL_STATUSES)}")

    effective = _clean_date(effective_date) if effective_date is not None else base.effective_date
    if not effective and fallback_date is not None:
        effective = fallback_date.astimezone(UTC).date().isoformat()

    return Provenance(
        source_type=chosen_type,
        effective_date=effective,
        version=_clean_text(version if version is not None else base.version, "version"),
        authority=_clean_text(authority if authority is not None else base.authority, "authority"),
        owner=_clean_text(owner if owner is not None else base.owner, "owner"),
        approval_status=chosen_status,
    )


def provenance_from_metadata(
    metadata: Mapping[str, str | None] | None, fallback_date: datetime | None = None
) -> Provenance:
    """Provenance recorded on a blob. Missing or unusable values fall back to safe defaults.

    A document uploaded before provenance existed is ``historical`` and ``unreviewed``, dated by when its file was
    last changed, so nothing is promoted by accident.
    """
    meta = metadata or {}
    values = {name: unquote(meta.get(f"{_PREFIX}{name}") or "") for name in _FIELDS}
    try:
        return build_provenance(fallback_date=fallback_date, **values)
    except ProvenanceError:
        return build_provenance(fallback_date=fallback_date)


# ── Ranking ──────────────────────────────────────────────


@dataclass(frozen=True)
class RankingPolicy:
    """How much provenance moves a result up or down. Weights multiply the search score."""

    tier_weights: Mapping[str, float]
    status_weights: Mapping[str, float]
    half_life_days: float  # age at which freshness is halfway to its floor; 0 turns freshness off
    freshness_floor: float  # the weight of an arbitrarily old document

    def weight(self, source_type: str, approval_status: str, effective_date: str, today: date) -> float:
        tier = self.tier_weights.get(source_type or DEFAULT_SOURCE_TYPE, 1.0)
        status = self.status_weights.get(approval_status or DEFAULT_APPROVAL_STATUS, 1.0)
        return tier * status * self.freshness(effective_date, today)

    def freshness(self, effective_date: str, today: date) -> float:
        if self.half_life_days <= 0:
            return 1.0
        try:
            age_days = max((today - date.fromisoformat(effective_date[:10])).days, 0)
        except ValueError:
            # No usable date (chunks indexed before provenance existed): neither fresh nor stale.
            return (1.0 + self.freshness_floor) / 2
        decay = math.pow(0.5, age_days / self.half_life_days)
        return self.freshness_floor + (1.0 - self.freshness_floor) * decay


def describe_source(doc: dict[str, Any]) -> str:
    """A one-line description of what kind of source a chunk or evidence entry came from.

    For example ``authoritative, approved, effective 2025-03-01, version 3, section "2. Security", page 4``.
    """
    pieces = []
    if doc.get("source_type"):
        pieces.append(str(doc["source_type"]).replace("_", " "))
    if doc.get("approval_status") and doc["approval_status"] != "unreviewed":
        pieces.append(str(doc["approval_status"]))
    if doc.get("effective_date"):
        pieces.append(f"effective {doc['effective_date']}")
    if doc.get("version"):
        pieces.append(f"version {doc['version']}")
    if doc.get("section"):
        pieces.append(f"section \"{doc['section']}\"")
    if doc.get("page"):
        pieces.append(f"page {doc['page']}")
    return ", ".join(pieces)


def parse_weights(text: str, label: str) -> dict[str, float]:
    """``"a=1.2,b=0.9"`` as a dict (for settings)."""
    weights: dict[str, float] = {}
    for part in filter(None, (p.strip() for p in text.split(","))):
        name, _, value = part.partition("=")
        try:
            weights[name.strip().lower()] = float(value)
        except ValueError:
            raise ValueError(f"{label}: '{part}' is not name=number") from None
    return weights
