"""Query builder for Azure AI Search with DLAC + segmentation filters.

Every search request MUST include:
1. Segmentation filters (client_id, project_id, solution_area)
2. Security trimming (user oid / group membership)
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def build_odata_filter(
    *,
    client_id: str,
    project_id: str,
    solution_area: str | None = None,
    user_oid: str,
    user_groups: list[str] | None = None,
) -> str:
    """Build an OData $filter expression enforcing segmentation + DLAC.

    Example output::

        client_id eq 'acme' and project_id eq 'proj-42'
        and solution_area eq 'cloud'
        and (oids/any(o: o eq 'user-oid-123')
             or groups/any(g: search.in(g, 'grp-1,grp-2')))
    """
    parts: list[str] = [
        f"client_id eq '{_escape(client_id)}'",
        f"project_id eq '{_escape(project_id)}'",
    ]

    if solution_area:
        parts.append(f"solution_area eq '{_escape(solution_area)}'")

    # DLAC: user must be in oids list OR share a group
    dlac_clauses: list[str] = [f"oids/any(o: o eq '{_escape(user_oid)}')"]
    if user_groups:
        group_csv = ",".join(_escape(g) for g in user_groups)
        dlac_clauses.append(f"groups/any(g: search.in(g, '{group_csv}'))")

    dlac_expr = " or ".join(dlac_clauses)
    parts.append(f"({dlac_expr})")

    full_filter = " and ".join(parts)
    logger.debug("OData filter: %s", full_filter)
    return full_filter


def build_search_options(
    *,
    client_id: str,
    project_id: str,
    solution_area: str | None = None,
    user_oid: str,
    user_groups: list[str] | None = None,
    top: int = 8,
) -> dict[str, Any]:
    """Return kwargs suitable for ``SearchClient.search()``."""
    return {
        "filter": build_odata_filter(
            client_id=client_id,
            project_id=project_id,
            solution_area=solution_area,
            user_oid=user_oid,
            user_groups=user_groups,
        ),
        "top": top,
        "select": ["id", "content", "title", "source_url", "solution_area"],
        "query_type": "semantic",
        "semantic_configuration_name": "default",
    }


def _escape(value: str) -> str:
    """Escape single quotes in OData string literals."""
    return value.replace("'", "''")
