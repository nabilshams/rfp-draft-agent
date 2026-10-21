# RFP Draft Agent

Azure AI Foundry code-first agent that ingests RFP documents, extracts requirements, drafts grounded responses using a curated knowledge base (Azure AI Search), and produces DOCX + XLSX outputs – with built-in guardrails for PII/pricing redaction and document-level access control (DLAC).

---

## Architecture

```
┌──────────────┐   POST /rfp/upload        ┌────────────────┐
│   RFP (PDF/  │ ──────────────────────────►│  Azure Blob    │
│   DOCX)      │                            │  Storage       │
└──────────────┘                            └───────┬────────┘
                                                    │
                   POST /rfp/extract                │
                ◄───────────────────────────────────┘
                        │
                        ▼
              ┌─────────────────┐        ┌──────────────────┐
              │  Requirement    │        │  Redaction Engine │
              │  Extraction     │───────►│  (YAML policy)   │
              └────────┬────────┘        └──────────────────┘
                       │
                       ▼
              ┌─────────────────┐   Hybrid query    ┌──────────────────┐
              │  Foundry Agent  │──────────────────►│  Azure AI Search │
              │  (GPT-4o)       │◄──────────────────│  (vector+keyword)│
              │  + AI Search    │   DLAC filters     │  + DLAC fields   │
              │    Tool         │                    └──────────────────┘
              └────────┬────────┘
                       │
              ┌────────┴────────┐
              ▼                 ▼
    ┌──────────────┐  ┌──────────────┐
    │  draft.docx  │  │ compliance.  │
    │  (python-    │  │ xlsx         │
    │   docx)      │  │ (openpyxl)   │
    └──────────────┘  └──────────────┘
```

## Features

| Feature | Details |
|---|---|
| **RFP Ingestion** | PDF (PyPDF2) and DOCX (python-docx) parsing; extracts sections, shall/must requirements, questions, evaluation criteria |
| **Knowledge Base** | Blob → chunk → embed → Azure AI Search index with segmentation + DLAC fields |
| **Grounded Drafting** | Azure AI Foundry Agent with AzureAISearchTool; hybrid (vector+semantic) retrieval; citations required |
| **Compliance Matrix** | XLSX with data validation (Yes/Partial/No); evidence + source URLs |
| **DOCX Output** | Template-based with placeholder replacement + appended structured content |
| **Redaction** | Configurable YAML policy; regex-based removal of pricing, PII, banking info; pre-gen + post-gen scans |
| **DLAC** | OData filters enforce `oids/groups` membership + `client_id/project_id/solution_area` segmentation |
| **Auth** | Entra ID JWT validation; extracts `oid` + `groups` claims; dev bypass mode |
| **Observability** | OpenTelemetry + Application Insights; spans for tool calls, redaction events, token usage |
| **Infra-as-Code** | Bicep for Search, Storage, App Insights, Managed Identity, Container Apps |

---

## Prerequisites

- Python 3.11+
- Azure subscription with:
  - Azure AI Foundry project (with a GPT-4o deployment)
  - Azure AI Search (Basic tier or higher)
  - Azure Blob Storage account
  - Application Insights
  - Entra ID app registration (for production auth)
- Docker (optional, for containerised deployment)
- [Azure Developer CLI (`azd`)](https://learn.microsoft.com/azure/developer/azure-developer-cli/install-azd) and Azure CLI (for deploying to Azure)

---

## Quick Start

### 1. Clone and install

```bash
git clone <repo-url> && cd rfp-draft-agent
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pip install -e ".[dev]"
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env with your Azure resource values
```

| Variable | Description |
|---|---|
| `FOUNDRY_PROJECT_ENDPOINT` | Azure AI Foundry project endpoint |
| `FOUNDRY_MODEL_DEPLOYMENT_NAME` | Model deployment name (default: `gpt-4o`) |
| `AZURE_AI_SEARCH_CONNECTION_NAME` | Search service connection name/ID |
| `AI_SEARCH_INDEX_NAME` | Search index name (default: `rfp-knowledge-base`) |
| `AZURE_STORAGE_ACCOUNT_URL` | Blob storage account URL |
| `STORAGE_KB_CONTAINER` | Container for knowledge base docs (default: `knowledge-base`) |
| `STORAGE_RFP_CONTAINER` | Container for RFP uploads (default: `rfp-uploads`) |
| `STORAGE_OUTPUT_CONTAINER` | Container for generated outputs (default: `outputs`) |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | App Insights connection string |
| `ENTRA_TENANT_ID` | Entra ID tenant ID (set to `dev` for local dev bypass) |
| `ENTRA_CLIENT_ID` | Entra ID client/app ID |
| `ENTRA_AUDIENCE` | JWT audience (defaults to client ID) |
| `EMBEDDING_MODEL_DEPLOYMENT_NAME` | Embedding model (default: `text-embedding-3-large`) |
| `PARALLELISM` | Number of Uvicorn worker processes when running via `python -m app.main` (default: `1`) |
| `SECTION_PARALLELISM` | Number of sections synthesized concurrently in `per_section` and map-reduce phase 2 (default: `1`) |

### 3. Generate the DOCX template

```bash
python scripts/create_template.py
```

### 4. Run the server

```bash
# Development (auth bypass)
ENTRA_TENANT_ID=dev uvicorn app.main:app --reload --port 8000

# Parallel workers from .env (set PARALLELISM, disable reload)
python -m app.main

# Or with Docker Compose (includes Azurite for local blob storage)
docker-compose up --build
```

### 5. Run tests

```bash
pytest tests/ -v
```

### 6. Run the demo

```bash
python scripts/demo.py --base-url http://localhost:8000
```

---

## API Reference

### `GET /health`
Liveness probe.

### `GET /metrics`
Basic operational counters (uploads, drafts, etc.).

### `POST /rfp/upload`
Upload an RFP document (PDF/DOCX).

```bash
curl -X POST http://localhost:8000/rfp/upload \
  -F "file=@my_rfp.pdf"
```

**Response:**
```json
{ "rfp_id": "abc123", "blob_name": "abc123/my_rfp.pdf", "filename": "my_rfp.pdf", "size_bytes": 52480 }
```

### `POST /rfp/extract`
Extract structured requirements from an uploaded RFP.

```bash
curl -X POST http://localhost:8000/rfp/extract \
  -H "Content-Type: application/json" \
  -d '{"rfp_id": "abc123", "filename": "my_rfp.pdf"}'
```

**Response:**
```json
{
  "sections": [...],
  "requirements": [
    { "id": "a1b2c3d4", "text": "The vendor shall ...", "category": "compliance", "priority": "mandatory", ... }
  ],
  "questions": ["Does your platform support ...?"],
  "evaluation_criteria": [...],
  "response_instructions": {}
}
```

### `POST /draft`
Generate a full RFP response draft (DOCX + XLSX).

```bash
curl -X POST http://localhost:8000/draft \
  -H "Content-Type: application/json" \
  -d '{
    "rfp_id": "abc123",
    "client_id": "acme",
    "project_id": "proj-42",
    "solution_area": "cloud",
    "tone": "professional"
  }'
```

**Response:**
```json
{
  "rfp_id": "abc123",
  "draft": { "executive_summary": "...", "solution_overview": "...", "responses": [...] },
  "outputs": {
    "docx_url": "https://...blob.core.windows.net/outputs/abc123/draft.docx",
    "xlsx_url": "https://...blob.core.windows.net/outputs/abc123/compliance.xlsx"
  },
  "elapsed_seconds": 42.3
}
```

### `POST /answer`
Answer a single question with grounded citations.

```bash
curl -X POST http://localhost:8000/answer \
  -H "Content-Type: application/json" \
  -d '{
    "question": "Does your platform support multi-tenant architecture?",
    "client_id": "acme",
    "project_id": "proj-42",
    "solution_area": "cloud"
  }'
```

### `POST /kb/ingest`
Ingest a knowledge-base document from Blob into the AI Search index.

```bash
curl -X POST http://localhost:8000/kb/ingest \
  -H "Content-Type: application/json" \
  -d '{
    "blob_name": "acme/proj-42/cloud/solution_guide.pdf",
    "client_id": "acme",
    "project_id": "proj-42",
    "solution_area": "cloud",
    "oids": ["user-oid-1"],
    "groups": ["group-id-1"]
  }'
```

### `GET /outputs/{rfp_id}/{filename}`
Download a generated output file.

```bash
curl -O http://localhost:8000/outputs/abc123/draft.docx
curl -O http://localhost:8000/outputs/abc123/compliance.xlsx
```

---

## Infrastructure Deployment

The app deploys to **Azure Container Apps** with the [Azure Developer CLI](https://learn.microsoft.com/azure/developer/azure-developer-cli/install-azd) (`azd`).

```bash
azd auth login
azd up          # prompts for environment name, subscription and region, then provisions + deploys
```

`azd up` runs `azd provision` (Bicep in `infra/`) followed by `azd deploy` (builds the image remotely in
Azure Container Registry, so local Docker is not required). It prints the app URL (`SERVICE_API_ENDPOINT_URL`).
Use `azd deploy` for code-only updates and `azd down --purge` to delete everything.

**What gets created** (resource group `rg-<environment>`):

| Resource | Purpose |
|---|---|
| Container Apps environment + app (1 vCPU / 2 GiB, single replica) | Hosts the API and UI |
| Container Registry (Basic) | Stores the image (pulled with managed identity) |
| AI Services account + Foundry project | Agents; `gpt-4o` and `text-embedding-3-small` deployments |
| AI Search (`basic`) | Knowledge-base index (created on first KB ingest) |
| Storage account | `knowledge-base`, `rfp-uploads`, `outputs` containers |
| Log Analytics + Application Insights | Logs and traces |
| User-assigned managed identity | Used by the app; has the data-plane roles below |

Keyless by design: storage, search and AI Services have local (key) auth disabled and the app uses
`DefaultAzureCredential`. The managed identity gets Storage Blob Data Contributor, Search Index Data
Contributor, Search Service Contributor, Azure AI User and Cognitive Services OpenAI User. The identity
running `azd` gets the same roles so you can run the app and scripts locally.

**Requirements:** an Azure subscription where you can create role assignments (Owner, or Contributor +
User Access Administrator), quota for `gpt-4o` (GlobalStandard, 30K TPM) and `text-embedding-3-small`
in the chosen region, and the `Microsoft.App`, `Microsoft.ContainerRegistry`, `Microsoft.CognitiveServices`,
`Microsoft.Search` resource providers registered.

**Optional settings** (`azd env set <NAME> <value>` before `azd up`/`azd provision`):

| Variable | Default | Effect |
|---|---|---|
| `ENTRA_TENANT_ID` | `dev` | `dev` disables authentication. Set your tenant ID (plus `ENTRA_CLIENT_ID`, optionally `ENTRA_AUDIENCE`) to enforce JWT validation. The built-in UI has no sign-in yet, so it needs bearer tokens from another client. |
| `ALLOWED_IP_RANGES` | _(empty = open)_ | Comma-separated CIDRs allowed to reach the app, e.g. `203.0.113.4/32,198.51.100.0/24`. |
| `BING_CONNECTION_ID` | _(empty)_ | Resource ID of an existing Grounding-with-Bing connection in the Foundry project. When empty, agents are created without Bing grounding. |

> **Security:** with the default `ENTRA_TENANT_ID=dev` the deployed app has **no authentication** and is
> reachable from the internet. Set `ALLOWED_IP_RANGES` or configure Entra before loading real RFP data.

After the first deploy, upload documents through the UI or `POST /kb/upload` + `POST /kb/ingest`; the
search index is created on first ingest. To run locally against the deployed resources, copy the values
from `azd env get-values` into `.env`.

---

## Knowledge Base Guidelines

Following the **"Less is More"** principle:

1. **Only index accurate, unique, relevant content** – remove conflicting or duplicative sources
2. **Segment by client/project/solution area** – use the blob path convention: `/kb/{client_id}/{project_id}/{solution_area}/`
3. **Sanitize before indexing** – the ingestion pipeline automatically redacts pricing/PII per `config/redaction_policy.yml`
4. **Rotate and refresh** – set `last_reviewed_date` and periodically remove stale content
5. **Maintain consistent tone** – curate source docs for professional, concise language

---

## Security Model

### Document-Level Access Control (DLAC)

Every search query includes an OData filter:

```
client_id eq '{client_id}'
AND project_id eq '{project_id}'
AND (oids/any(o: o eq '{user_oid}') OR groups/any(g: search.in(g, '{group_ids}')))
```

This ensures users can only retrieve documents they are authorized to see.

### Redaction Guardrails

| Stage | Action |
|---|---|
| **Ingestion** | Regex-based redaction of pricing, PII, banking info before indexing |
| **Pre-generation** | User input and retrieved snippets are scanned and redacted |
| **Post-generation** | Agent output is scanned; if sensitive patterns found, output is redacted and flagged for human review |

Configure rules in [config/redaction_policy.yml](config/redaction_policy.yml).

---

## Project Structure

```
rfp-draft-agent/
├── app/
│   ├── main.py                    # FastAPI entry point
│   ├── config.py                  # Settings from env vars
│   ├── auth/
│   │   └── entra.py               # Entra ID JWT validation
│   ├── agents/
│   │   ├── foundry_agent.py       # Foundry agent orchestration
│   │   ├── prompts.py             # System instructions + JSON schemas
│   │   └── retrieval.py           # DLAC + segmentation query builder
│   ├── ingestion/
│   │   ├── rfp_extract.py         # PDF/DOCX parsing + requirement extraction
│   │   ├── kb_ingest.py           # Blob → chunk → embed → index
│   │   └── redaction.py           # Regex-based redaction engine
│   ├── outputs/
│   │   ├── docx_writer.py         # DOCX generation
│   │   └── xlsx_writer.py         # XLSX compliance matrix
│   └── telemetry/
│       └── tracing.py             # OpenTelemetry + App Insights
├── config/
│   └── redaction_policy.yml       # Redaction rules (YAML)
├── infra/
│   ├── main.bicep                 # azd entry point (subscription scope, creates the resource group)
│   ├── resources.bicep            # Foundry, Search, Storage, ACR, Container Apps, RBAC
│   └── main.parameters.json       # Maps azd environment values to Bicep parameters
├── azure.yaml                     # azd service definition (Container App, remote build)
├── templates/
│   └── rfp_response_template.docx # DOCX template (generate with scripts/create_template.py)
├── scripts/
│   ├── create_template.py         # Generate the DOCX template
│   └── demo.py                    # End-to-end demo script
├── tests/
│   ├── test_redaction.py
│   ├── test_requirement_extraction.py
│   ├── test_security_filters.py
│   └── test_output_generation.py
├── .github/workflows/ci.yml
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── pyproject.toml
├── .env.example
└── README.md
```

---

## License

MIT
