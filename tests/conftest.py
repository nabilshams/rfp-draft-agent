"""Shared test setup: provide the minimum settings the app requires so tests run without a .env file."""

import os

os.environ.setdefault("FOUNDRY_PROJECT_ENDPOINT", "https://test.services.ai.azure.com")
os.environ.setdefault("AZURE_AI_SEARCH_CONNECTION_NAME", "test-search")
os.environ.setdefault("AZURE_STORAGE_ACCOUNT_URL", "https://teststorage.blob.core.windows.net")
# Most tests exercise endpoints without authentication; auth tests build their own Settings.
os.environ["AUTH_MODE"] = "disabled"
# Keep tests independent of a developer's local .env.
os.environ["REDACTION_CUSTOMER_NAMES"] = ""
os.environ["BING_CONNECTION_ID"] = ""

# Fixtures shared by the API tests (in-memory Azure fakes and an HTTP client).
import pytest  # noqa: E402

from app import azure_clients, kb_refs  # noqa: E402
from app.ledger import store as ledger_store  # noqa: E402
from app.services import jobs  # noqa: E402

from .test_authorization_api import client, use_settings, world  # noqa: E402, F401


@pytest.fixture(autouse=True)
def no_real_azure_clients(monkeypatch):
    """A test that forgets to fake an Azure service fails loudly instead of calling the real one."""

    def refuse(name):
        def _refuse(*_args, **_kwargs):
            raise AssertionError(f"test tried to create a real {name}; fake it (see the `world` fixture)")

        return _refuse

    azure_clients.reset()
    jobs.reset()
    kb_refs.refs.forget()
    ledger_store.reset_memory()
    monkeypatch.setattr("azure.storage.blob.BlobServiceClient", refuse("BlobServiceClient"))
    monkeypatch.setattr("azure.cosmos.CosmosClient", refuse("CosmosClient"))
    monkeypatch.setattr("azure.search.documents.SearchClient", refuse("SearchClient"))
    monkeypatch.setattr("azure.search.documents.indexes.SearchIndexClient", refuse("SearchIndexClient"))
    monkeypatch.setattr("azure.ai.agents.AgentsClient", refuse("AgentsClient"))
    monkeypatch.setattr("openai.AzureOpenAI", refuse("AzureOpenAI"))
    yield
    jobs.reset()
    azure_clients.reset()
