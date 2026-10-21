"""Application configuration – loaded from environment variables / .env."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Central configuration.  Values are read from env-vars or a .env file."""

    # ── Azure AI Foundry ──────────────────────────────────
    foundry_project_endpoint: str = Field(..., alias="FOUNDRY_PROJECT_ENDPOINT")
    foundry_model_deployment_name: str = Field("gpt-4o", alias="FOUNDRY_MODEL_DEPLOYMENT_NAME")
    foundry_project_name: str = Field("", alias="FOUNDRY_PROJECT_NAME")

    # ── Azure AI Search ───────────────────────────────────
    azure_ai_search_connection_name: str = Field(..., alias="AZURE_AI_SEARCH_CONNECTION_NAME")
    azure_ai_search_endpoint: str = Field("", alias="AZURE_AI_SEARCH_ENDPOINT")
    ai_search_index_name: str = Field("rfp-knowledge-base", alias="AI_SEARCH_INDEX_NAME")

    # ── Azure Blob Storage ────────────────────────────────
    azure_storage_account_url: str = Field(..., alias="AZURE_STORAGE_ACCOUNT_URL")
    storage_kb_container: str = Field("knowledge-base", alias="STORAGE_KB_CONTAINER")
    storage_rfp_container: str = Field("rfp-uploads", alias="STORAGE_RFP_CONTAINER")
    storage_output_container: str = Field("outputs", alias="STORAGE_OUTPUT_CONTAINER")

    # ── Application Insights ──────────────────────────────
    applicationinsights_connection_string: str = Field("", alias="APPLICATIONINSIGHTS_CONNECTION_STRING")

    # ── Entra ID JWT ──────────────────────────────────────
    entra_tenant_id: str = Field(..., alias="ENTRA_TENANT_ID")
    entra_client_id: str = Field(..., alias="ENTRA_CLIENT_ID")
    entra_audience: str = Field("", alias="ENTRA_AUDIENCE")

    # ── Optional ──────────────────────────────────────────
    embedding_model_deployment_name: str = Field(
        "text-embedding-3-large", alias="EMBEDDING_MODEL_DEPLOYMENT_NAME"
    )
    parallelism: int = Field(1, alias="PARALLELISM", ge=1)
    section_parallelism: int = Field(1, alias="SECTION_PARALLELISM", ge=1)
    # Longest a single agent run may take before it is cancelled.
    run_timeout_seconds: int = Field(600, alias="RUN_TIMEOUT_SECONDS", ge=30)
    log_level: str = Field("INFO", alias="LOG_LEVEL")

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "populate_by_name": True,
        "extra": "ignore",
    }


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return a cached Settings singleton."""
    global _settings
    if _settings is None:
        _settings = Settings()  # type: ignore[call-arg]
    return _settings
