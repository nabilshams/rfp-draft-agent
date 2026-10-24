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
    # ── Claim verification: a second, independent pass that judges each claim against the evidence it cites ──
    verify_answers: bool = Field(True, alias="VERIFY_ANSWERS")
    # Empty = the same deployment as the writer. A different deployment reduces the errors the two would share
    # (it must already exist in the Foundry account; it is not created for you).
    verifier_model_deployment_name: str = Field("", alias="VERIFIER_MODEL_DEPLOYMENT_NAME")
    # Prices (USD per million tokens) when the verifier's deployment costs differently; empty = the writer's prices
    verifier_input_price_per_million: float | None = Field(None, alias="VERIFIER_INPUT_PRICE_PER_MILLION", ge=0)
    verifier_output_price_per_million: float | None = Field(None, alias="VERIFIER_OUTPUT_PRICE_PER_MILLION", ge=0)
    # List prices in USD per million tokens, used only to estimate the cost shown in telemetry and reports.
    # Set them to your model's and region's price; they are not read from Azure.
    model_input_price_per_million: float = Field(2.5, alias="MODEL_INPUT_PRICE_PER_MILLION", ge=0)
    model_output_price_per_million: float = Field(10.0, alias="MODEL_OUTPUT_PRICE_PER_MILLION", ge=0)
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
    storage_jobs_container: str = Field("draft-jobs", alias="STORAGE_JOBS_CONTAINER")

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

    # ── Project ledger (Azure Cosmos DB for NoSQL) ─────────
    # Where projects, requirements, answer versions and the audit trail live. Empty COSMOS_ENDPOINT leaves the ledger
    # off (its endpoints answer 503). LEDGER_BACKEND=memory keeps everything in this process only, for local
    # development: it is lost on restart and not shared between instances, so never use it for real work.
    cosmos_endpoint: str = Field("", alias="COSMOS_ENDPOINT")
    cosmos_database: str = Field("rfp-ledger", alias="COSMOS_DATABASE")
    cosmos_container: str = Field("ledger", alias="COSMOS_CONTAINER")
    ledger_backend: Literal["", "cosmos", "memory"] = Field("", alias="LEDGER_BACKEND")
    ledger_max_requirements: int = Field(1000, alias="LEDGER_MAX_REQUIREMENTS", ge=1, le=5000)

    # ── Never invent: requirements that are routed to people, not drafted ──
    # Terms separated by ";". ``word*`` matches any word starting that way; other terms match as whole words or
    # phrases (never inside a hyphenated word: "cost-effective" is not about cost). A requirement whose text, category
    # or section matches is not sent to the model: it is given to the commercial or legal team (see app.agents.states).
    route_commercial_terms: str = Field(
        "price*; pricing; cost; costs; costing; fee; fees; rate card*; discount*; payment*; invoic*; quote; quotes; "
        "quotation*; budget*; total cost of ownership; tco; per user per month; licensing model",
        alias="ROUTE_COMMERCIAL_TERMS",
    )
    route_legal_terms: str = Field(
        "indemnif*; liabilit*; warrant*; governing law; jurisdiction*; terms and conditions; intellectual property; "
        "termination for*; insurance; force majeure; non-disclosure; nda; liquidated damages; penalt*; "
        "dispute resolution; contractual; contract terms; master services agreement; data processing agreement; "
        "service credit*",
        alias="ROUTE_LEGAL_TERMS",
    )

    # ── Knowledge-base retrieval ──────────────────────────
    search_top_k: int = Field(5, alias="SEARCH_TOP_K", ge=1, le=20)  # documents retrieved per query
    # Documents scoring below this are dropped. Uses the semantic reranker score (0-4) when the
    # semantic ranker is on, otherwise the search score. 0 keeps everything.
    search_min_score: float = Field(0.0, alias="SEARCH_MIN_SCORE", ge=0)
    # Re-rank with Azure AI Search's semantic ranker (a metered feature; needs the Basic tier or above).
    search_semantic_ranker: bool = Field(False, alias="SEARCH_SEMANTIC_RANKER")
    # Cap on documents shared across many requirements in one prompt (full draft, section narratives).
    search_max_context_docs: int = Field(15, alias="SEARCH_MAX_CONTEXT_DOCS", ge=1, le=50)
    # Ranking by provenance (see app.ingestion.provenance). Weights multiply the search score; 1.0 changes nothing.
    # Candidates are fetched SEARCH_CANDIDATE_FACTOR times wider than SEARCH_TOP_K, re-ordered, then cut to top-k.
    kb_tier_weights: str = Field(
        "authoritative=1.3,approved_answer=1.15,historical=1.0,external=0.9", alias="KB_TIER_WEIGHTS"
    )
    kb_status_weights: str = Field(
        "approved=1.0,unreviewed=1.0,draft=0.8,superseded=0.5", alias="KB_STATUS_WEIGHTS"
    )
    # Age at which a document's freshness weight is halfway between 1.0 and the floor; 0 turns freshness off.
    kb_freshness_half_life_days: float = Field(730, alias="KB_FRESHNESS_HALF_LIFE_DAYS", ge=0)
    kb_freshness_floor: float = Field(0.7, alias="KB_FRESHNESS_FLOOR", gt=0, le=1)
    search_candidate_factor: int = Field(3, alias="SEARCH_CANDIDATE_FACTOR", ge=1, le=10)

    # ── Optional ──────────────────────────────────────────
    embedding_model_deployment_name: str = Field(
        "text-embedding-3-large", alias="EMBEDDING_MODEL_DEPLOYMENT_NAME"
    )
    parallelism: int = Field(1, alias="PARALLELISM", ge=1)
    section_parallelism: int = Field(1, alias="SECTION_PARALLELISM", ge=1)
    # Longest a single agent run may take before it is cancelled.
    run_timeout_seconds: int = Field(600, alias="RUN_TIMEOUT_SECONDS", ge=30)
    # Background draft jobs (POST /drafts): how many run at once in this process, how often a running job
    # records that it is alive, and how long without that before another request declares it lost.
    job_workers: int = Field(2, alias="JOB_WORKERS", ge=1, le=16)
    job_heartbeat_seconds: float = Field(15.0, alias="JOB_HEARTBEAT_SECONDS", gt=0)
    job_stale_seconds: float = Field(120.0, alias="JOB_STALE_SECONDS", gt=0)
    # Unfinished jobs one user may have in this process at a time.
    job_max_active_per_user: int = Field(3, alias="JOB_MAX_ACTIVE_PER_USER", ge=1)
    log_level: str = Field("INFO", alias="LOG_LEVEL")
    host: str = Field("0.0.0.0", alias="HOST")
    port: int = Field(8000, alias="PORT", ge=1, le=65535)
    uvicorn_reload: bool = Field(False, alias="UVICORN_RELOAD")

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

    @model_validator(mode="after")
    def _check_ranking_weights(self) -> Settings:
        from app.ingestion.provenance import parse_weights

        parse_weights(self.kb_tier_weights, "KB_TIER_WEIGHTS")
        parse_weights(self.kb_status_weights, "KB_STATUS_WEIGHTS")
        return self

    def ranking_policy(self):
        """How retrieval weighs source type, approval status and age (see ``app.ingestion.provenance``)."""
        from app.ingestion.provenance import RankingPolicy, parse_weights

        return RankingPolicy(
            tier_weights=parse_weights(self.kb_tier_weights, "KB_TIER_WEIGHTS"),
            status_weights=parse_weights(self.kb_status_weights, "KB_STATUS_WEIGHTS"),
            half_life_days=self.kb_freshness_half_life_days,
            freshness_floor=self.kb_freshness_floor,
        )

    @property
    def admin_oid_set(self) -> frozenset[str]:
        return frozenset(oid.strip() for oid in self.admin_oids.split(",") if oid.strip())

    @property
    def search_endpoint(self) -> str:
        return self.azure_ai_search_endpoint or f"https://{self.azure_ai_search_connection_name}.search.windows.net"

    @property
    def agents_endpoint(self) -> str:
        """The project-scoped endpoint of the Foundry Agent Service."""
        base = self.foundry_project_endpoint.rstrip("/")
        base = base.removesuffix("/openai")  # legacy form
        return f"{base}/api/projects/{self.foundry_project_name}" if self.foundry_project_name else base

    @property
    def openai_endpoint(self) -> str:
        """The Azure OpenAI endpoint of the Foundry account (used for embeddings)."""
        endpoint = self.foundry_project_endpoint.rstrip("/")
        for suffix in (".services.ai.azure.com", ".cognitiveservices.azure.com"):
            if suffix in endpoint:
                return endpoint.split(suffix)[0] + ".openai.azure.com"
        return endpoint



_settings: Settings | None = None


def get_settings() -> Settings:
    """Return a cached Settings singleton."""
    global _settings
    if _settings is None:
        _settings = Settings()  # type: ignore[call-arg]
    return _settings
