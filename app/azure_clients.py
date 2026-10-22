"""Shared Azure SDK clients.

Each client (and the credential behind it, so access tokens are cached) is created once per process on first
use and reused by every request. The SDK classes are looked up when a client is first created, not at import
time, so importing the application does not need Azure to be configured.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from app.config import Settings

if TYPE_CHECKING:
    from azure.ai.agents import AgentsClient
    from azure.cosmos import ContainerProxy
    from azure.search.documents import SearchClient
    from azure.search.documents.indexes import SearchIndexClient
    from azure.storage.blob import BlobServiceClient, ContainerClient
    from openai import AzureOpenAI

_lock = threading.RLock()
_cache: dict[tuple, Any] = {}


def _cached(key: tuple, factory: Callable[[], Any]) -> Any:
    with _lock:
        if key not in _cache:
            _cache[key] = factory()
        return _cache[key]


def reset() -> None:
    """Forget every client (for tests, and after configuration changes)."""
    with _lock:
        _cache.clear()


def credential():
    from azure.identity import DefaultAzureCredential

    return _cached(("credential",), DefaultAzureCredential)


def blob_service(settings: Settings) -> BlobServiceClient:
    def create():
        from azure.storage.blob import BlobServiceClient

        return BlobServiceClient(account_url=settings.azure_storage_account_url, credential=credential())

    return _cached(("blob", settings.azure_storage_account_url), create)


def container(settings: Settings, name: str) -> ContainerClient:
    return blob_service(settings).get_container_client(name)


def search_client(settings: Settings) -> SearchClient:
    def create():
        from azure.search.documents import SearchClient

        return SearchClient(
            endpoint=settings.search_endpoint, index_name=settings.ai_search_index_name, credential=credential()
        )

    return _cached(("search", settings.search_endpoint, settings.ai_search_index_name), create)


def cosmos_container(settings: Settings) -> ContainerProxy:
    """The ledger's Cosmos DB container (created by the infrastructure; the app never creates it)."""

    def create():
        from azure.cosmos import CosmosClient

        client = CosmosClient(settings.cosmos_endpoint, credential=credential())
        return client.get_database_client(settings.cosmos_database).get_container_client(settings.cosmos_container)

    return _cached(("cosmos", settings.cosmos_endpoint, settings.cosmos_database, settings.cosmos_container), create)


def search_index_client(settings: Settings) -> SearchIndexClient:
    def create():
        from azure.search.documents.indexes import SearchIndexClient

        return SearchIndexClient(endpoint=settings.search_endpoint, credential=credential())

    return _cached(("search-index", settings.search_endpoint), create)


def openai_client(settings: Settings) -> AzureOpenAI:
    def create():
        from azure.identity import get_bearer_token_provider
        from openai import AzureOpenAI

        return AzureOpenAI(
            azure_endpoint=settings.openai_endpoint,
            azure_ad_token_provider=get_bearer_token_provider(
                credential(), "https://cognitiveservices.azure.com/.default"
            ),
            api_version="2024-10-21",
        )

    return _cached(("openai", settings.openai_endpoint), create)


def agents_client(settings: Settings) -> AgentsClient:
    def create():
        from azure.ai.agents import AgentsClient

        return AgentsClient(endpoint=settings.agents_endpoint, credential=credential())

    return _cached(("agents", settings.agents_endpoint), create)
