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
from .test_authorization_api import client, use_settings, world  # noqa: E402, F401
