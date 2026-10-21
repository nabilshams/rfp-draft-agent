"""Knowledge-base ingestion pipeline.

Reads documents from Azure Blob Storage, chunks them, embeds with Azure
OpenAI, and indexes into Azure AI Search – with redaction applied before
indexing.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from typing import Any

import tiktoken
from azure.identity import DefaultAzureCredential
from azure.search.documents import SearchClient
from azure.search.documents.indexes import SearchIndexClient
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
from azure.storage.blob import BlobServiceClient

from app.config import get_settings
from app.ingestion.redaction import get_redaction_engine
from app.ingestion.rfp_extract import extract_text

logger = logging.getLogger(__name__)

_tokenizer = tiktoken.get_encoding("cl100k_base")

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
        SimpleField(
            name="last_reviewed_date",
            type=SearchFieldDataType.DateTimeOffset,
            filterable=True,
            sortable=True,
        ),
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
    credential = DefaultAzureCredential()
    # Extract the search endpoint from the connection name – in production this
    # would come from the Foundry project connections.  For standalone usage,
    # the env var ``AZURE_AI_SEARCH_ENDPOINT`` can be provided.
    import os

    search_endpoint = os.environ.get(
        "AZURE_AI_SEARCH_ENDPOINT",
        f"https://{settings.azure_ai_search_connection_name}.search.windows.net",
    )
    client = SearchIndexClient(endpoint=search_endpoint, credential=credential)
    index = get_index_schema(settings.ai_search_index_name)
    client.create_or_update_index(index)
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


def _chunk_text(text: str, max_tokens: int = 1000, overlap_tokens: int = 150) -> list[str]:
    """Split *text* into chunks of roughly *max_tokens* with overlap."""
    tokens = _tokenizer.encode(text)
    chunks: list[str] = []
    start = 0
    while start < len(tokens):
        end = min(start + max_tokens, len(tokens))
        chunk_tokens = tokens[start:end]
        chunks.append(_tokenizer.decode(chunk_tokens))
        start += max_tokens - overlap_tokens
    return chunks


def _chunk_by_headings(text: str, max_tokens: int = 1000) -> list[str]:
    """Try to split on markdown-style or numbered headings first,
    falling back to token-based chunking if a section is too long."""
    import re

    heading_re = re.compile(r"^(?:#{1,4}\s|(?:\d+\.)+\s)", re.MULTILINE)
    matches = list(heading_re.finditer(text))
    if len(matches) < 2:
        return _chunk_text(text, max_tokens)

    raw_sections: list[str] = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        raw_sections.append(text[m.start() : end].strip())

    chunks: list[str] = []
    for sec in raw_sections:
        if len(_tokenizer.encode(sec)) > max_tokens:
            chunks.extend(_chunk_text(sec, max_tokens))
        else:
            chunks.append(sec)
    return chunks


# ── Embedding helper ──────────────────────────────────────


def _embed_texts(texts: list[str]) -> list[list[float]]:
    """Call Azure OpenAI embedding model via the Azure OpenAI client."""
    from azure.identity import DefaultAzureCredential, get_bearer_token_provider
    from openai import AzureOpenAI

    settings = get_settings()
    token_provider = get_bearer_token_provider(
        DefaultAzureCredential(),
        "https://cognitiveservices.azure.com/.default",
    )
    # Use the .openai.azure.com endpoint for embeddings
    openai_endpoint = settings.foundry_project_endpoint.rstrip("/")
    # Derive the openai endpoint from the services endpoint
    if ".services.ai.azure.com" in openai_endpoint:
        base = openai_endpoint.split(".services.ai.azure.com")[0]
        openai_endpoint = base + ".openai.azure.com"
    elif ".cognitiveservices.azure.com" in openai_endpoint:
        base = openai_endpoint.split(".cognitiveservices.azure.com")[0]
        openai_endpoint = base + ".openai.azure.com"

    client = AzureOpenAI(
        azure_endpoint=openai_endpoint,
        azure_ad_token_provider=token_provider,
        api_version="2024-10-21",
    )
    response = client.embeddings.create(
        model=settings.embedding_model_deployment_name,
        input=texts,
    )
    return [item.embedding for item in response.data]


# Public name: query embedding in app.agents.kb_search uses the same model as ingestion.
embed_texts = _embed_texts


# ── Main ingest function ─────────────────────────────────


def ingest_blob(
    blob_name: str,
    client_id: str,
    project_id: str,
    solution_area: str,
    oids: list[str] | None = None,
    groups: list[str] | None = None,
    redaction_level: str = "standard",
) -> int:
    """Download a single blob, redact, chunk, embed, and index it.

    Returns the number of chunks indexed.
    """
    settings = get_settings()
    credential = DefaultAzureCredential()

    # 1. Download blob
    _ensure_index_once()
    blob_service = BlobServiceClient(account_url=settings.azure_storage_account_url, credential=credential)
    container = blob_service.get_container_client(settings.storage_kb_container)
    blob_data = container.download_blob(blob_name).readall()

    filename = blob_name.rsplit("/", 1)[-1]
    raw_text = extract_text(filename, blob_data)
    logger.info("Extracted %d chars from %s", len(raw_text), blob_name)

    # 2. Redact
    engine = get_redaction_engine()
    result = engine.redact(raw_text)
    sanitized = result.sanitized_text
    if result.redaction_count:
        logger.info("Redacted %d patterns from %s", result.redaction_count, blob_name)

    # 3. Chunk
    chunks = _chunk_by_headings(sanitized)
    if not chunks:
        logger.warning("No chunks produced for %s", blob_name)
        return 0

    # 4. Embed
    vectors = _embed_texts(chunks)

    # 5. Build documents
    source_url = f"{settings.azure_storage_account_url}/{settings.storage_kb_container}/{blob_name}"
    documents: list[dict[str, Any]] = []
    for i, (chunk, vec) in enumerate(zip(chunks, vectors)):
        doc_id = hashlib.md5(f"{blob_name}:{i}".encode()).hexdigest()
        documents.append(
            {
                "id": doc_id,
                "content": chunk,
                "contentVector": vec,
                "title": filename,
                "source_url": source_url,
                "client_id": client_id,
                "project_id": project_id,
                "solution_area": solution_area,
                "oids": oids or [],
                "groups": groups or [],
                "redaction_level": redaction_level,
                "last_reviewed_date": datetime.now(UTC).isoformat(),
            }
        )

    # 6. Upload to index
    import os

    search_endpoint = os.environ.get(
        "AZURE_AI_SEARCH_ENDPOINT",
        f"https://{settings.azure_ai_search_connection_name}.search.windows.net",
    )
    search_client = SearchClient(
        endpoint=search_endpoint,
        index_name=settings.ai_search_index_name,
        credential=credential,
    )
    search_client.upload_documents(documents)
    logger.info("Indexed %d chunks for %s", len(documents), blob_name)
    return len(documents)
