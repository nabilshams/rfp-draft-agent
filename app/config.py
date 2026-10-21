"""Application configuration – loaded from environment variables / .env."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator
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

    # ── Authentication ────────────────────────────────────
    # disabled – no authentication (local development / demos only; logs a warning at startup)
    # easyauth – Azure Container Apps built-in authentication authenticates every request and passes
    #            the identity in X-MS-CLIENT-PRINCIPAL headers (the app must only be reachable through it)
    # jwt      – the app validates Entra ID bearer tokens itself (needs the ENTRA_* settings)
    auth_mode: Literal["disabled", "easyauth", "jwt"] = Field("jwt", alias="AUTH_MODE")
    entra_tenant_id: str = Field("", alias="ENTRA_TENANT_ID")
    entra_client_id: str = Field("", alias="ENTRA_CLIENT_ID")
    entra_audience: str = Field("", alias="ENTRA_AUDIENCE")

    # ── Authorisation and limits ──────────────────────────
    # Comma-separated object IDs of users who can manage every RFP and KB document.
    # When AUTH_MODE=disabled everyone is treated as an administrator.
    admin_oids: str = Field("", alias="ADMIN_OIDS")
    max_upload_mb: int = Field(25, alias="MAX_UPLOAD_MB", ge=1)
    # Requests per user per minute on the expensive endpoints (0 disables the limit).
    rate_limit_per_minute: int = Field(60, alias="RATE_LIMIT_PER_MINUTE", ge=0)

    # ── Guardrails ────────────────────────────────────────
    # Customer names to redact from inputs, knowledge-base documents and generated output, separated by ";".
    # Kept in the environment (a secret in Azure), never in the repository.
    redaction_customer_names: str = Field("", alias="REDACTION_CUSTOMER_NAMES")
    # Resource ID of a Grounding-with-Bing connection. Web search is only available when this is set AND a
    # request opts in with allow_web_search; text sent to Bing leaves the Azure compliance boundary.
    bing_connection_id: str = Field("", alias="BING_CONNECTION_ID")

    # ── Knowledge-base retrieval ──────────────────────────
    search_top_k: int = Field(5, alias="SEARCH_TOP_K", ge=1, le=20)  # documents retrieved per query
    # Documents scoring below this are dropped. Uses the semantic reranker score (0-4) when the
    # semantic ranker is on, otherwise the search score. 0 keeps everything.
    search_min_score: float = Field(0.0, alias="SEARCH_MIN_SCORE", ge=0)
    # Re-rank with Azure AI Search's semantic ranker (a metered feature; needs the Basic tier or above).
    search_semantic_ranker: bool = Field(False, alias="SEARCH_SEMANTIC_RANKER")
    # Cap on documents shared across many requirements in one prompt (full draft, section narratives).
    search_max_context_docs: int = Field(15, alias="SEARCH_MAX_CONTEXT_DOCS", ge=1, le=50)

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

    @model_validator(mode="after")
    def _check_auth_settings(self) -> Settings:
        if self.auth_mode == "jwt" and (self.entra_tenant_id in ("", "dev") or not self.entra_client_id):
            raise ValueError(
                "AUTH_MODE=jwt requires ENTRA_TENANT_ID (a real tenant ID) and ENTRA_CLIENT_ID. "
                "To run without authentication (local development only) set AUTH_MODE=disabled; "
                "ENTRA_TENANT_ID=dev is no longer supported."
            )
        return self

    @property
    def admin_oid_set(self) -> frozenset[str]:
        return frozenset(oid.strip() for oid in self.admin_oids.split(",") if oid.strip())


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return a cached Settings singleton."""
    global _settings
    if _settings is None:
        _settings = Settings()  # type: ignore[call-arg]
    return _settings
