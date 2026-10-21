"""Knowledge-base retrieval and citation checks.

Retrieval is *hybrid*: each query runs as a keyword search and a vector search over the chunk
embeddings, fused by Azure AI Search, optionally re-ranked by the semantic ranker. Every query carries
the segmentation and document-level access filters (see :mod:`app.agents.retrieval`), and the vector
part uses ``preFilter`` so those filters are applied *before* the nearest-neighbour cut-off rather
than after it (otherwise users could lose relevant results, or see fewer than they are entitled to).

Retrieval degrades instead of failing a draft: without embeddings it falls back to keyword search,
without the semantic ranker to plain hybrid, and a missing index simply yields no documents.

Citations: the model may only cite sources it was given (or that Bing grounding returned).
:func:`sanitize_citations` removes everything else from a generated result.
"""

from __future__ import annotations

import inspect
import logging
import re
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote

from azure.core.exceptions import HttpResponseError, ResourceNotFoundError
from azure.identity import DefaultAzureCredential
from azure.search.documents import SearchClient
from azure.search.documents.models import VectorFilterMode, VectorizedQuery

from app.agents.retrieval import build_odata_filter
from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

_VECTOR_FIELD = "contentVector"
_SELECT = ["content", "title", "source_url"]
_EMBED_BATCH_SIZE = 16
_MAX_QUERY_CHARS = 2000
_MAX_DOC_CHARS = 1500

_search_client: SearchClient | None = None
_client_lock = threading.Lock()


def search_endpoint(settings: Settings) -> str:
    return settings.azure_ai_search_endpoint or f"https://{settings.azure_ai_search_connection_name}.search.windows.net"


def _get_search_client() -> SearchClient:
    """One shared client (and credential, so tokens are cached) for all searches."""
    global _search_client
    if _search_client is None:
        with _client_lock:
            if _search_client is None:
                settings = get_settings()
                _search_client = SearchClient(
                    endpoint=search_endpoint(settings),
                    index_name=settings.ai_search_index_name,
                    credential=DefaultAzureCredential(),
                )
    return _search_client


# ── Embeddings ───────────────────────────────────────────


def embed_queries(queries: list[str]) -> list[list[float] | None]:
    """Embed search queries (batched). Entries are ``None`` when embeddings are unavailable.

    A failure here must not fail the draft: callers fall back to keyword search.
    """
    if not queries:
        return []
    from app.ingestion.kb_ingest import embed_texts

    vectors: list[list[float] | None] = []
    try:
        for start in range(0, len(queries), _EMBED_BATCH_SIZE):
            batch = [q[:_MAX_QUERY_CHARS] or " " for q in queries[start : start + _EMBED_BATCH_SIZE]]
            vectors.extend(embed_texts(batch))
    except Exception as exc:
        logger.warning("Could not embed search queries; using keyword search only: %s", exc)
        return [None] * len(queries)
    return vectors


# ── Search ───────────────────────────────────────────────


def requirement_query(requirement: dict[str, Any]) -> str:
    """The search query for a requirement: its text (falling back to its id)."""
    return (requirement.get("text") or requirement.get("requirement_text") or str(requirement.get("id", ""))).strip()


def _score(doc: dict[str, Any]) -> float:
    """The score a minimum-score threshold applies to: reranker score when present, else search score."""
    reranker = doc.get("reranker_score")
    return float(reranker if reranker is not None else doc.get("score") or 0.0)


def _vector_query(vector: list[float], k: int) -> VectorizedQuery:
    """A vector query for the chunk embeddings, asking for *k* nearest neighbours.

    The parameter is called ``k`` in azure-search-documents 11.7 and ``k_nearest_neighbors`` in 11.6;
    an unknown name is silently ignored by the SDK, so pick the one the installed version has.
    """
    name = "k" if "k" in inspect.signature(VectorizedQuery.__init__).parameters else "k_nearest_neighbors"
    return VectorizedQuery(vector=vector, fields=_VECTOR_FIELD, **{name: k})


def search_kb(
    *,
    query: str,
    vector: list[float] | None = None,
    client_id: str,
    project_id: str,
    solution_area: str | None,
    user_oid: str,
    user_groups: list[str] | None,
    top_k: int | None = None,
    min_score: float | None = None,
    semantic: bool | None = None,
) -> list[dict[str, Any]]:
    """Search the knowledge base for *query* with access filters; returns chunk dicts.

    Each result has ``content``, ``title``, ``source_url``, ``score`` and ``reranker_score``.
    """
    settings = get_settings()
    top_k = top_k or settings.search_top_k
    min_score = settings.search_min_score if min_score is None else min_score
    use_semantic = settings.search_semantic_ranker if semantic is None else semantic

    options: dict[str, Any] = {
        "search_text": query,
        "filter": build_odata_filter(
            client_id=client_id,
            project_id=project_id,
            solution_area=solution_area,
            user_oid=user_oid,
            user_groups=user_groups,
        ),
        "top": top_k,
        "select": _SELECT,
    }
    if vector:
        options["vector_queries"] = [_vector_query(vector, max(top_k * 2, 10))]
        options["vector_filter_mode"] = VectorFilterMode.PRE_FILTER  # apply access filters before the kNN cut-off
    if use_semantic:
        options["query_type"] = "semantic"
        options["semantic_configuration_name"] = "default"

    client = _get_search_client()
    results: Iterable[dict[str, Any]]
    while True:
        try:
            results = list(client.search(**options))
            break
        except ResourceNotFoundError:
            logger.warning("Search index not found; the knowledge base is empty")
            return []
        except HttpResponseError as exc:
            # Degrade rather than fail: drop the semantic ranker first, then the vector query.
            if "query_type" in options:
                logger.warning("Semantic ranking unavailable (%s); retrying without it", exc.message)
                options.pop("query_type")
                options.pop("semantic_configuration_name", None)
            elif "vector_queries" in options:
                logger.warning("Vector search failed (%s); retrying with keyword search only", exc.message)
                options.pop("vector_queries")
                options.pop("vector_filter_mode", None)
            else:
                raise

    docs = [
        {
            "content": r.get("content", ""),
            "title": r.get("title", ""),
            "source_url": r.get("source_url", ""),
            "score": r.get("@search.score", 0),
            "reranker_score": r.get("@search.reranker_score"),
        }
        for r in results
    ]
    if min_score > 0:
        docs = [d for d in docs if _score(d) >= min_score]
    logger.info(
        "Search (%s%s) returned %d results for query: %.60s",
        "hybrid" if "vector_queries" in options else "keyword",
        "+semantic" if "query_type" in options else "",
        len(docs),
        query,
    )
    return docs


def merge_docs(doc_lists: list[list[dict[str, Any]]], limit: int) -> list[dict[str, Any]]:
    """Combine results of several queries: best-ranked first, taken in turns from each list.

    Interleaving gives every query a fair share of the *limit*; duplicates are dropped.
    """
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    depth = max((len(docs) for docs in doc_lists), default=0)
    for rank in range(depth):
        for docs in doc_lists:
            if rank >= len(docs):
                continue
            doc = docs[rank]
            key = (_normalize_url(doc.get("source_url", "")), doc.get("content", "")[:200])
            if key in seen:
                continue
            seen.add(key)
            merged.append(doc)
            if len(merged) >= limit:
                return merged
    return merged


# ── Prompt context ───────────────────────────────────────


def format_context(docs: list[dict[str, Any]]) -> str:
    """Format retrieved documents as the sources block of an agent prompt."""
    if not docs:
        return (
            "\n## Internal Knowledge Base\n"
            "[No matching documents were found in the internal knowledge base for this request. "
            "Do not cite or invent internal sources.]\n"
        )
    parts = ["## Internal Knowledge Base Sources\n"]
    for i, doc in enumerate(docs, 1):
        parts.append(f"### KB Source {i}: {doc.get('title', '')}")
        parts.append(f"**URL:** {doc.get('source_url', '')}")
        parts.append(f"**Content:**\n{doc.get('content', '')[:_MAX_DOC_CHARS]}\n")
    return "\n".join(parts)


@dataclass
class RetrievedContext:
    """What was retrieved for a request: the prompt block and the URLs the model may cite."""

    docs: list[dict[str, Any]] = field(default_factory=list)

    @property
    def block(self) -> str:
        return format_context(self.docs)

    @property
    def urls(self) -> set[str]:
        return {d["source_url"] for d in self.docs if d.get("source_url")}

    @property
    def found(self) -> bool:
        return bool(self.docs)


class RequirementRetriever:
    """Searches the knowledge base for each requirement separately, on demand.

    All queries are embedded up front in one batched call. A failed search degrades that requirement to
    "no KB evidence" (logged as an error), but repeated consecutive failures are treated as systemic
    (for example a misconfigured endpoint) and raised, so a whole draft can't quietly go without evidence.
    """

    MAX_CONSECUTIVE_FAILURES = 3

    def __init__(
        self,
        requirements: list[dict[str, Any]],
        *,
        client_id: str,
        project_id: str,
        solution_area: str | None,
        user_oid: str,
        user_groups: list[str] | None,
    ) -> None:
        self._queries = [requirement_query(r) for r in requirements]
        self._vectors = embed_queries(self._queries)
        self._access = {
            "client_id": client_id,
            "project_id": project_id,
            "solution_area": solution_area,
            "user_oid": user_oid,
            "user_groups": user_groups,
        }
        self._consecutive_failures = 0

    def context(self, index: int) -> RetrievedContext:
        try:
            docs = search_kb(query=self._queries[index], vector=self._vectors[index], **self._access)
        except Exception as exc:
            self._consecutive_failures += 1
            logger.error("Knowledge-base search failed for requirement %d: %s", index + 1, exc)
            if self._consecutive_failures >= self.MAX_CONSECUTIVE_FAILURES:
                raise
            return RetrievedContext()
        self._consecutive_failures = 0
        return RetrievedContext(docs)

    def all(self) -> list[RetrievedContext]:
        return [self.context(i) for i in range(len(self._queries))]


def retrieve_for_requirements(requirements: list[dict[str, Any]], **access: Any) -> list[RetrievedContext]:
    """Retrieve documents for every requirement now (see :class:`RequirementRetriever`)."""
    return RequirementRetriever(requirements, **access).all()


def merge_contexts(contexts: list[RetrievedContext], limit: int | None = None) -> RetrievedContext:
    """One context built from several (e.g. all requirements of a section)."""
    limit = limit or get_settings().search_max_context_docs
    return RetrievedContext(merge_docs([c.docs for c in contexts], limit))


# ── Citations ────────────────────────────────────────────

_MD_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def _normalize_url(url: str) -> str:
    return unquote(url.strip()).rstrip("/")


def _is_allowed(url: str, allowed: set[str]) -> bool:
    target = _normalize_url(url)
    if not target:
        return False
    # A link target is cut at the first ')', so a file name containing parentheses yields a truncated
    # URL. Accept such a prefix, but only when it includes a path (scheme://host/...), never a bare host.
    return any(known == target or (target.count("/") >= 3 and known.startswith(target)) for known in allowed)


def _strip_unretrieved_links(text: str, allowed: set[str]) -> str:
    def check(match: re.Match) -> str:
        url = match.group(2)
        if url.startswith("#") or _is_allowed(url, allowed):
            return match.group(0)
        logger.warning("Removing citation to a source that was not retrieved: %s", url)
        return match.group(1)  # keep the link text, drop the URL

    return _MD_LINK.sub(check, text)


def cited_urls(value: Any) -> set[str]:
    """Every URL a generated result cites: markdown link targets and ``evidence`` source URLs."""
    urls: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, str):
            urls.update(m.group(2) for m in _MD_LINK.finditer(node))
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            if isinstance(node.get("source_url"), str) and node["source_url"]:
                urls.add(node["source_url"])
            for item in node.values():
                walk(item)

    walk(value)
    return urls


def sanitize_citations(value: Any, allowed_urls: Iterable[str]) -> Any:
    """Remove citations to sources the model was never given.

    Walks a generated result: markdown links in every string whose target is not allowed become plain
    text, and ``evidence`` entries whose ``source_url`` is not allowed are dropped. Allowed URLs are
    the retrieved KB documents plus any URL returned by web grounding.
    """
    allowed = {_normalize_url(u) for u in allowed_urls if u}

    def walk(node: Any) -> Any:
        if isinstance(node, str):
            return _strip_unretrieved_links(node, allowed)
        if isinstance(node, list):
            return [walk(item) for item in node]
        if isinstance(node, dict):
            cleaned = {k: walk(v) for k, v in node.items()}
            evidence = node.get("evidence")
            if isinstance(evidence, list):
                kept = []
                for item in evidence:
                    if isinstance(item, dict) and _is_allowed(str(item.get("source_url", "")), allowed):
                        kept.append(walk(item))
                    else:
                        logger.warning("Removing evidence that cites a source that was not retrieved: %s", item)
                cleaned["evidence"] = kept
            return cleaned
        return node

    return walk(value)
