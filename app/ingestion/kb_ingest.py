"""Knowledge-base ingestion pipeline.

Reads documents from Azure Blob Storage, chunks them, embeds with Azure
OpenAI, and indexes into Azure AI Search – with redaction applied before
indexing.
"""

from __future__ import annotations

import functools
import hashlib
import logging
import re
from bisect import bisect_right
from dataclasses import dataclass
from typing import Any

import tiktoken
from azure.search.documents.indexes.models import (
    HnswAlgorithmConfiguration,
    SearchableField,
    SearchField,
    SearchFieldDataType,
    SearchIndex,
    SemanticConfiguration,
    SemanticField,
    SemanticPrioritizedFields,
    SemanticSearch,
    SimpleField,
    VectorSearch,
    VectorSearchProfile,
)

from app import azure_clients
from app.config import get_settings
from app.ingestion.provenance import Provenance
from app.ingestion.redaction import get_redaction_engine
from app.ingestion.rfp_extract import extract_text, parse_pdf

logger = logging.getLogger(__name__)

_MAX_SECTION_CHARS = 150

@functools.lru_cache(maxsize=1)
def _tokenizer() -> tiktoken.Encoding:
    """Loaded on first use: it can need a download, which importing the application must not trigger."""
    return tiktoken.get_encoding("cl100k_base")

# ── Index schema ──────────────────────────────────────────


def get_index_schema(index_name: str) -> SearchIndex:
    """Build the Azure AI Search index definition."""
    fields = [
        SimpleField(name="id", type=SearchFieldDataType.String, key=True, filterable=True),
        SearchableField(name="content", type=SearchFieldDataType.String),
        SearchField(
            name="contentVector",
            type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
            searchable=True,
            vector_search_dimensions=1536,
            vector_search_profile_name="default-profile",
        ),
        SimpleField(name="title", type=SearchFieldDataType.String, retrievable=True, filterable=True),
        SimpleField(name="source_url", type=SearchFieldDataType.String, retrievable=True),
        SimpleField(name="client_id", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="project_id", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="solution_area", type=SearchFieldDataType.String, filterable=True),
        SimpleField(
            name="oids",
            type=SearchFieldDataType.Collection(SearchFieldDataType.String),
            filterable=True,
        ),
        SimpleField(
            name="groups",
            type=SearchFieldDataType.Collection(SearchFieldDataType.String),
            filterable=True,
        ),
        SimpleField(name="redaction_level", type=SearchFieldDataType.String, filterable=True),
        # Kept because an index field can't be dropped in place; it now holds the document's effective date.
        SimpleField(
            name="last_reviewed_date",
            type=SearchFieldDataType.DateTimeOffset,
            filterable=True,
            sortable=True,
        ),
        # Provenance (see app.ingestion.provenance) and where in the document the chunk came from
        SimpleField(name="source_type", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SimpleField(name="authority", type=SearchFieldDataType.String, filterable=True),
        SimpleField(
            name="effective_date", type=SearchFieldDataType.DateTimeOffset, filterable=True, sortable=True
        ),
        SimpleField(name="version", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="owner", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="approval_status", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SimpleField(name="section", type=SearchFieldDataType.String),
        SimpleField(name="page", type=SearchFieldDataType.Int32, filterable=True),
    ]

    vector_search = VectorSearch(
        profiles=[VectorSearchProfile(name="default-profile", algorithm_configuration_name="default-hnsw")],
        algorithms=[HnswAlgorithmConfiguration(name="default-hnsw")],
    )

    # Used when SEARCH_SEMANTIC_RANKER is on; defining it costs nothing.
    semantic_search = SemanticSearch(
        default_configuration_name="default",
        configurations=[
            SemanticConfiguration(
                name="default",
                prioritized_fields=SemanticPrioritizedFields(
                    title_field=SemanticField(field_name="title"),
                    content_fields=[SemanticField(field_name="content")],
                ),
            )
        ],
    )

    return SearchIndex(name=index_name, fields=fields, vector_search=vector_search, semantic_search=semantic_search)


def ensure_index_exists() -> None:
    """Create the search index, or update it to the current definition."""
    settings = get_settings()
    index = get_index_schema(settings.ai_search_index_name)
    azure_clients.search_index_client(settings).create_or_update_index(index)
    logger.info("Search index '%s' ensured.", settings.ai_search_index_name)


_index_ready = False


def _ensure_index_once() -> None:
    """Make sure the index exists before the first ingest in this process.

    Failures are logged and retried on the next ingest, so a role assignment that has not propagated
    yet, or an index created by hand with a different definition, does not block ingestion.
    """
    global _index_ready
    if _index_ready:
        return
    try:
        ensure_index_exists()
        _index_ready = True
    except Exception as exc:
        logger.warning("Could not create or update the search index (continuing): %s", exc)


# ── Chunking ──────────────────────────────────────────────


@dataclass(frozen=True)
class Chunk:
    text: str
    start: int = 0  # offset of the chunk in the document text
    section: str = ""
    page: int | None = None


_HEADING_RE = re.compile(r"^(?:#{1,4}\s|(?:\d+\.)+\s)", re.MULTILINE)


def _chunk_text_spans(text: str, max_tokens: int = 1000, overlap_tokens: int = 150) -> list[tuple[str, int]]:
    """Split *text* into chunks of roughly *max_tokens* with overlap, each with its offset in *text*."""
    tokens = _tokenizer().encode(text)
    overlap_tokens = min(overlap_tokens, max_tokens // 2)  # the window must always move forward
    chunks: list[tuple[str, int]] = []
    start = 0
    while start < len(tokens):
        end = min(start + max_tokens, len(tokens))
        offset = len(_tokenizer().decode(tokens[:start])) if start else 0
        chunks.append((_tokenizer().decode(tokens[start:end]), min(offset, len(text))))
        start += max_tokens - overlap_tokens
    return chunks


def _chunk_text(text: str, max_tokens: int = 1000, overlap_tokens: int = 150) -> list[str]:
    """Split *text* into chunks of roughly *max_tokens* with overlap."""
    return [chunk for chunk, _ in _chunk_text_spans(text, max_tokens, overlap_tokens)]


def _heading_line(text: str) -> str:
    line = text.split("\n", 1)[0].strip().lstrip("#").strip()
    return line[:_MAX_SECTION_CHARS]


def chunk_document(
    text: str, page_starts: list[tuple[int, int]] | None = None, max_tokens: int = 1000
) -> list[Chunk]:
    """Chunks of *text* that remember their section heading and (for paged documents) page.

    Text is split on markdown-style or numbered headings first, falling back to token-based chunking when a
    section is too long or the text has no headings. *page_starts* are ``(offset, page number)`` pairs.
    Text before the first heading is kept.
    """
    matches = list(_HEADING_RE.finditer(text))
    headings = [(m.start(), _heading_line(text[m.start() :])) for m in matches]
    pages = page_starts or []
    page_offsets = [offset for offset, _ in pages]

    def section_at(offset: int) -> str:
        index = bisect_right([h[0] for h in headings], offset) - 1
        return headings[index][1] if index >= 0 else ""

    def page_at(offset: int) -> int | None:
        index = bisect_right(page_offsets, offset) - 1
        return pages[index][1] if index >= 0 else None

    def make(piece: str, start: int) -> Chunk:
        return Chunk(piece, start, section_at(start), page_at(start))

    if len(matches) < 2:
        return [make(piece, start) for piece, start in _chunk_text_spans(text, max_tokens)]

    bounds = [m.start() for m in matches]
    spans = [(bounds[i], bounds[i + 1] if i + 1 < len(bounds) else len(text)) for i in range(len(bounds))]
    if bounds[0] > 0 and text[: bounds[0]].strip():
        spans.insert(0, (0, bounds[0]))

    chunks: list[Chunk] = []
    for start, end in spans:
        piece = text[start:end].strip()
        if not piece:
            continue
        offset = start + (len(text[start:end]) - len(text[start:end].lstrip()))
        if len(_tokenizer().encode(piece)) > max_tokens:
            chunks.extend(make(sub, offset + sub_start) for sub, sub_start in _chunk_text_spans(piece, max_tokens))
        else:
            chunks.append(make(piece, offset))
    return chunks


def _chunk_by_headings(text: str, max_tokens: int = 1000) -> list[str]:
    """The chunk texts of :func:`chunk_document`."""
    return [chunk.text for chunk in chunk_document(text, max_tokens=max_tokens)]


# ── Embedding helper ──────────────────────────────────────


def _embed_texts(texts: list[str]) -> list[list[float]]:
    """Call the Azure OpenAI embedding model."""
    settings = get_settings()
    response = azure_clients.openai_client(settings).embeddings.create(
        model=settings.embedding_model_deployment_name,
        input=texts,
    )
    return [item.embedding for item in response.data]


# Public name: query embedding in app.agents.kb_search uses the same model as ingestion.
embed_texts = _embed_texts


# ── Main ingest function ─────────────────────────────────


def _redacted_text_and_pages(filename: str, data: bytes) -> tuple[str, list[tuple[int, int]]]:
    """The document's redacted text and, for a PDF, where each page starts in it as ``(offset, page)``.

    Pages are redacted one by one (and joined with a blank line, as the plain-text reader does) so that page
    positions stay exact whatever redaction changes the length of.
    """
    engine = get_redaction_engine()
    if not filename.lower().endswith(".pdf"):
        result = engine.redact(extract_text(filename, data))
        if result.redaction_count:
            logger.info("Redacted %d patterns from %s", result.redaction_count, filename)
        return result.sanitized_text, []

    document = parse_pdf(data)
    bounds = [offset for offset, _ in document.anchors] + [len(document.text) + 2]
    parts: list[str] = []
    page_starts: list[tuple[int, int]] = []
    length = 0
    redactions = 0
    for (offset, label), end in zip(document.anchors, bounds[1:]):
        result = engine.redact(document.text[offset : end - 2])
        redactions += result.redaction_count
        start = length + (2 if parts else 0)
        page_starts.append((start, int(label.removeprefix("p. "))))
        parts.append(result.sanitized_text)
        length = start + len(result.sanitized_text)
    if redactions:
        logger.info("Redacted %d patterns from %s", redactions, filename)
    return "\n\n".join(parts), page_starts


def ingest_blob(
    blob_name: str,
    client_id: str,
    project_id: str,
    solution_area: str,
    oids: list[str] | None = None,
    groups: list[str] | None = None,
    redaction_level: str = "standard",
    provenance: Provenance | None = None,
) -> int:
    """Download a single blob, redact, chunk, embed, and index it.

    *provenance* (source type, dates, version, ...) is stored on every chunk; without it the document is indexed
    as ``historical`` and ``unreviewed``. Returns the number of chunks indexed.
    """
    settings = get_settings()
    provenance = provenance or Provenance()

    # 1. Download blob
    _ensure_index_once()
    container = azure_clients.container(settings, settings.storage_kb_container)
    blob_data = container.download_blob(blob_name).readall()

    filename = blob_name.rsplit("/", 1)[-1]

    # 2. Read and redact
    sanitized, page_starts = _redacted_text_and_pages(filename, blob_data)
    logger.info("Extracted %d chars from %s", len(sanitized), blob_name)

    # 3. Chunk
    chunks = chunk_document(sanitized, page_starts)
    if not chunks:
        logger.warning("No chunks produced for %s", blob_name)
        return 0

    # 4. Embed
    vectors = _embed_texts([chunk.text for chunk in chunks])

    # 5. Build documents
    source_url = f"{settings.azure_storage_account_url}/{settings.storage_kb_container}/{blob_name}"
    documents: list[dict[str, Any]] = []
    for i, (chunk, vec) in enumerate(zip(chunks, vectors)):
        doc_id = hashlib.md5(f"{blob_name}:{i}".encode()).hexdigest()
        documents.append(
            {
                "id": doc_id,
                "content": chunk.text,
                "contentVector": vec,
                "title": filename,
                "source_url": source_url,
                "client_id": client_id,
                "project_id": project_id,
                "solution_area": solution_area,
                "oids": oids or [],
                "groups": groups or [],
                "redaction_level": redaction_level,
                "section": chunk.section,
                "page": chunk.page,
                **provenance.to_index_fields(),
            }
        )

    # 6. Upload to index
    azure_clients.search_client(settings).upload_documents(documents)
    logger.info("Indexed %d chunks for %s", len(documents), blob_name)
    return len(documents)
