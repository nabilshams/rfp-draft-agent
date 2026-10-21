"""Shared test setup: provide the minimum settings the app requires so tests run without a .env file."""

import os

os.environ.setdefault("FOUNDRY_PROJECT_ENDPOINT", "https://test.services.ai.azure.com")
os.environ.setdefault("AZURE_AI_SEARCH_CONNECTION_NAME", "test-search")
os.environ.setdefault("AZURE_STORAGE_ACCOUNT_URL", "https://teststorage.blob.core.windows.net")
os.environ.setdefault("ENTRA_CLIENT_ID", "test-client-id")
# Tests rely on the dev auth bypass.
os.environ["ENTRA_TENANT_ID"] = "dev"
