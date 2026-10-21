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
| **Redaction** | Configurable YAML policy; regex rules for pricing, PII and banking info plus a maintained customer-name list; applied to uploads, requests, retrieved chunks and output |
| **DLAC** | OData filters enforce `oids/groups` membership + `client_id/project_id/solution_area` segmentation |
| **Auth** | Three modes via `AUTH_MODE`: `disabled` (local/demo), `easyauth` (Container Apps built-in sign-in), `jwt` (validates Entra bearer tokens); extracts `oid` + `groups` |
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
| `AUTH_MODE` | `disabled`, `easyauth` or `jwt` (default `jwt`). `disabled` turns authentication off and logs a warning at startup; use it for local development only. See [Authentication](#authentication) |
| `ENTRA_TENANT_ID` | Entra ID tenant ID (`jwt` mode). The old `dev` value is no longer supported; use `AUTH_MODE=disabled` |
| `ENTRA_CLIENT_ID` | Entra ID client/app ID (`jwt` mode) |
| `ENTRA_AUDIENCE` | JWT audience (`jwt` mode; defaults to client ID) |
| `EMBEDDING_MODEL_DEPLOYMENT_NAME` | Embedding model (default: `text-embedding-3-large`) |
| `PARALLELISM` | Number of Uvicorn worker processes when running via `python -m app.main` (default: `1`) |
| `SECTION_PARALLELISM` | Number of sections synthesized concurrently in `per_section` and map-reduce phase 2 (default: `1`) |
| `RUN_TIMEOUT_SECONDS` | Longest a single agent run may take before it is cancelled and reported as `timed_out` (default: `600`) |
| `SEARCH_TOP_K` | Knowledge-base documents retrieved per requirement or question (default: `5`) |
| `SEARCH_MIN_SCORE` | Drop weaker matches: the semantic reranker score (0-4) when `SEARCH_SEMANTIC_RANKER` is on, otherwise the search score (default: `0`, keep everything) |
| `SEARCH_SEMANTIC_RANKER` | Re-rank results with Azure AI Search's semantic ranker. A metered feature that needs the Basic tier or above; off by default (default: `false`) |
| `SEARCH_MAX_CONTEXT_DOCS` | Cap on documents shared by many requirements in one prompt: full drafts and section narratives (default: `15`) |
| `ADMIN_OIDS` | Comma-separated object IDs of administrators (see [Access model](#access-model)); empty by default |
| `MAX_UPLOAD_MB` | Largest accepted upload (default: `25`) |
| `RATE_LIMIT_PER_MINUTE` | Per-user requests per minute on the expensive endpoints (default: `60`, `0` = off) |

### 3. Generate the DOCX template

```bash
python scripts/create_template.py
```

### 4. Run the server

```bash
# Development (no authentication)
AUTH_MODE=disabled uvicorn app.main:app --reload --port 8000

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
Basic operational counters (uploads, drafts, etc.). Requires authentication (unless `AUTH_MODE=disabled`).

### `POST /rfp/upload`
Upload an RFP document (PDF/DOCX/XLSX/CSV, up to `MAX_UPLOAD_MB`). The uploader becomes its owner; only the
owner (or an admin) can extract, draft from or download outputs of it. See [Access model](#access-model).

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

Add `"allow_web_search": true` to let this one request use Bing grounding (see [Web search](#web-search-bing-grounding)).

### `POST /kb/upload`
Upload a reference document (PDF/DOCX), store it and index it. The document is **private to you** unless you
share it with groups you belong to.

```bash
curl -X POST http://localhost:8000/kb/upload \
  -F "file=@solution_guide.pdf" \
  -F "client_id=acme" -F "project_id=proj-42" -F "solution_area=cloud" \
  -F "share_with_groups=group-id-1,group-id-2"   # optional; default: private
```

Stored as `client_id/project_id/solution_area/filename`. Uploading the same name again replaces the document
and its sharing; only its owner (or an admin) can do that.

### `POST /kb/ingest`
Re-ingest a KB document you own. Who can retrieve it comes from the sharing recorded on the document, never
from the request (sending `oids` or `groups` is rejected with 422). An admin ingesting a document that has no
recorded owner becomes its owner.

```bash
curl -X POST http://localhost:8000/kb/ingest \
  -H "Content-Type: application/json" \
  -d '{ "blob_name": "acme/proj-42/cloud/solution_guide.pdf" }'
```

### `GET /kb/list`, `DELETE /kb/delete?blob_name=…`, `POST /kb/reindex`
`list` returns only documents you own or that are shared with one of your groups. `delete` is for the owner or
an admin (a document you can't see returns 404; one you can see but don't own returns 403). `reindex`
(admins only) re-indexes every document with the access recorded on it and skips documents that have no owner.

### `GET /outputs/{rfp_id}/{filename}`
Download a generated output file (`draft.docx` or `compliance.xlsx`) of an RFP you own.

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
| `AUTH_MODE` | `disabled` | `disabled` = no authentication (demo only). `easyauth` = users sign in with Entra ID through Container Apps built-in authentication; see [Authentication](#authentication). `jwt` = the app validates bearer tokens (needs `ENTRA_CLIENT_ID`; `ENTRA_TENANT_ID` defaults to your tenant). |
| `ALLOWED_IP_RANGES` | _(empty = open)_ | Comma-separated CIDRs allowed to reach the app, e.g. `203.0.113.4/32,198.51.100.0/24`. |
| `ADMIN_OIDS` | _(empty)_ | Object IDs of administrators who can manage every RFP and KB document. `scripts/setup_auth.py` sets it to the signed-in user. See [Access model](#access-model). |
| `BING_CONNECTION_ID` | _(empty)_ | Resource ID of an existing Grounding-with-Bing connection in the Foundry project. When set, users can opt in to web search per request; see [Web search](#web-search-bing-grounding) for the data-flow caveat. When empty, web search is unavailable. |
| `REDACTION_CUSTOMER_NAMES` | _(empty)_ | Semicolon-separated customer names to redact everywhere, e.g. `Contoso Ltd;Fabrikam`. Stored as a Container Apps secret. See [Redaction guardrails](#redaction-guardrails). |

> **Security:** with the default `AUTH_MODE=disabled` the deployed app has **no authentication** and is
> reachable from the internet; the app logs a warning at startup. Turn on [authentication](#authentication)
> or set `ALLOWED_IP_RANGES` before loading real RFP data.

### Authentication

The recommended setup for Azure is **Easy Auth** (Container Apps built-in authentication): the platform
signs users in with Entra ID before any request reaches the app, so the UI needs no sign-in code.

```bash
azd provision                    # creates the app (authentication is disabled at this point)
python scripts/setup_auth.py     # creates the Entra app registration and stores its values in the azd env
azd provision                    # applies AUTH_MODE=easyauth and the authentication settings
```

`setup_auth.py --dry-run` shows what it would change without changing anything, and re-running it keeps the
existing registration and secret (`--rotate-secret` creates a new secret). It needs permission to create app
registrations in your tenant. The client secret is stored in the azd environment (`.azure/`, git-ignored)
and as a Container Apps secret.

How it works and what to know:

- In `easyauth` mode the app trusts the `X-MS-CLIENT-PRINCIPAL*` identity headers. The platform removes
  those headers from external requests, so they only exist when Container Apps set them. Every request except
  `/health` (container probes) is authenticated by the platform; anonymous browsers are redirected to sign in.
  Never run `AUTH_MODE=easyauth` anywhere that is not behind Container Apps authentication.
- `azd provision` refuses `easyauth` / `jwt` without `ENTRA_CLIENT_ID` (and, for `easyauth`,
  `ENTRA_CLIENT_SECRET`), so a half-configured deployment can't be created.
- When switching an existing deployment from `disabled` to `easyauth`, there is a short window while the new
  revision starts before the authentication settings are applied. Set `ALLOWED_IP_RANGES` first if that matters.
- Users are identified by their object ID. Group claims are used for document access only when the platform
  passes them; resolving group membership for users in many groups (the "group overage" case) is not implemented.
- `/metrics` and `/ws/logs` require authentication in `easyauth` and `jwt` modes; only `/health` is open.
- API clients can use `AUTH_MODE=jwt` instead and send `Authorization: Bearer <Entra access token>`. Signing keys
  are cached for an hour and refetched (rate limited) when a token uses an unknown key, so key rotation is handled.

After the first deploy, upload documents through the UI or `POST /kb/upload` + `POST /kb/ingest`; the
search index is created on first ingest. To run locally against the deployed resources, copy the values
from `azd env get-values` into `.env`.

**Runtime notes**

- Endpoints that call Azure SDKs are plain `def` handlers, so FastAPI runs them in a worker thread pool and
  the event loop (including `/health`) stays responsive during long drafts. The app runs one process and
  one replica because the live log stream and `/metrics` counters are held in memory.
- Each agent call uses a temporary Foundry thread that is deleted when the call finishes.
- Container Apps ingress has a fixed [request timeout of 240 seconds](https://learn.microsoft.com/azure/container-apps/ingress-overview).
  A single non-streaming `POST /draft` over a large RFP can exceed that and be cut off by the platform even
  though the app keeps working. `POST /draft/stream` returns results as each requirement completes; whether
  very long streams are also subject to the same limit has not been tested.

---

## Retrieval and citations

**How context is retrieved.** Each requirement (or question) is searched on its own, so it is grounded on the
documents that match *its* text, not a shared sample of the RFP. A search is *hybrid*: a keyword query and a
vector query over the chunk embeddings, fused by Azure AI Search. The vector part uses `preFilter`, so the
segmentation and per-user access filters apply before the nearest-neighbour cut-off. All query embeddings are
computed in one batched call. Section narratives reuse the documents retrieved for their own requirements.

- The optional **semantic ranker** (`SEARCH_SEMANTIC_RANKER=true`) re-ranks results. It is a metered Azure AI
  Search feature, so it is off by default; the index already defines the `default` semantic configuration.
- Retrieval **degrades instead of failing a draft**: without embeddings it falls back to keyword search, without
  the semantic ranker to plain hybrid, and a missing index counts as an empty knowledge base. A single failed
  search leaves that requirement without evidence (logged as an error); three failures in a row are raised.
- The index is created or updated automatically before the first ingest in each process, so a fresh deployment
  needs no manual index setup.
- Each response carries `kb_evidence_found`. When it is `false`, the prompt tells the model that no internal
  source matched (it must not cite or invent one), and the UI shows a warning on that requirement.

**Citations are checked.** A model may only cite sources it was given: the retrieved KB documents, plus URLs
returned by Bing grounding when it is used for that request. After every generation (full draft, per-requirement answers,
section narratives and `/answer`) markdown links to any other URL are reduced to plain text and `evidence`
entries that cite any other URL are dropped. URLs are compared ignoring a trailing slash and percent-encoding.

**Measuring retrieval quality.** `python scripts/eval_retrieval.py evals/retrieval_cases.example.jsonl
--client-id <id> --project-id <id> --oid <object id>` runs your own cases (a query plus the documents that
should come back) in keyword, hybrid and semantic mode and prints hit rate, recall@k and MRR. Searches run with
the access rights of the user you give, so pick someone who can read the documents the cases refer to. The
example file only illustrates the format; write cases for your own knowledge base.

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

### Access model

- **Ownership.** An RFP or KB document belongs to whoever uploaded it, recorded in the blob's metadata
  (`owner_oid`, plus `shared_groups` for KB documents).
- **RFPs** are private: only the owner (or an admin) can extract, draft from, or download outputs of one.
- **KB documents** are private by default. The owner can share one with groups they belong to; members of
  those groups can retrieve it and see it in `GET /kb/list`. Only the owner (or an admin) can re-ingest,
  replace or delete it. Nobody can grant access to people or groups outside their own.
- **Admins** are the object IDs in `ADMIN_OIDS` (comma-separated). They can manage every RFP and document,
  re-index the knowledge base and replace the shared DOCX template (`POST /template/upload`). With
  `AUTH_MODE=disabled` everyone is treated as an admin. `scripts/setup_auth.py` makes the person running it
  the first admin; otherwise `azd env set ADMIN_OIDS <object-id>` (find yours with
  `az ad signed-in-user show --query id -o tsv`).
- **Documents with no recorded owner** (uploaded before ownership was recorded) are admin-only. An admin can
  claim one with `POST /kb/ingest`; RFPs can be re-uploaded.
- A resource you aren't allowed to see is reported as `404`, not `403`, so its existence isn't revealed.
- **Input checks.** `client_id`, `project_id`, `solution_area` and `rfp_id` must be 1-64 plain characters
  (letters, digits, `.` `_` `-`, inner spaces); file names may not contain path separators or control
  characters; only `draft.docx` and `compliance.xlsx` can be downloaded. Violations return 422/400.
- **Limits.** Uploads are capped at `MAX_UPLOAD_MB` (default 25; 413 above it) and each user is limited to
  `RATE_LIMIT_PER_MINUTE` (default 60; 0 disables) requests per minute across the draft, answer, upload,
  extract-ai, ingest, reindex and template endpoints (429 with `Retry-After`). The limiter is in-process, which
  matches the single-replica deployment. The file is parsed by the server before the size check runs, so this
  is a safeguard against accidents, not against deliberate flooding; put a gateway in front for that.

### Document-Level Access Control (DLAC)

Every search query includes an OData filter:

```
client_id eq '{client_id}'
AND project_id eq '{project_id}'
AND (oids/any(o: o eq '{user_oid}') OR groups/any(g: search.in(g, '{group_ids}')))
```

This ensures users can only retrieve documents they are authorized to see.

### Redaction guardrails

Redaction (rules in [config/redaction_policy.yml](config/redaction_policy.yml)) masks pricing, emails, phone
numbers, national IDs, IBANs, card and routing numbers, street addresses and **customer names**. Every path
into a model goes through one module, `app/ingestion/pipeline.py`:

| Stage | Action |
|---|---|
| **Ingestion** | KB documents are redacted before they are chunked and indexed |
| **Retrieval** | Retrieved chunks are redacted again, so a rule or customer name added after a document was indexed still applies to it (URLs are left exact so citations still verify) |
| **RFP files** | Requirements extracted from an uploaded RFP (DOCX/PDF text, spreadsheet rows, AI extraction input) are redacted |
| **Request bodies** | `question`, `context`, `prompt`, `requirements`, `responses` and `section_narratives` are redacted when the request is validated, so a client can't bypass redaction by sending its own requirements. Identifier, URL and enum fields are left alone |
| **Output** | The agent's reply is scanned; matches are redacted and the response is returned (flagged in `_meta.violations_redacted`) |

- **Customer names** are a maintained list, not a pattern: set `REDACTION_CUSTOMER_NAMES` to a
  semicolon-separated list (`azd env set REDACTION_CUSTOMER_NAMES "Contoso Ltd;Fabrikam"`). It is stored as a
  Container Apps **secret**, because this repository is public and the list is business-sensitive. Matching is
  case-insensitive on whole words; names are never written to the logs. Names your own company uses
  legitimately can be listed under `customer_allowlist` in the policy file.
- **Known limits.** Regex rules are heuristics: unusual formats are missed and a few non-sensitive strings may
  be masked. Output that trips the scanner is masked, not withheld; reviewing or blocking it with an agent is
  a planned follow-up. Redaction is not a substitute for reviewing a draft before it is sent.

### Web search (Bing grounding)

Off by default and **opt-in per request** (`allow_web_search: true` on `/draft`, `/draft/stream` and `/answer`,
a checkbox in the UI that only appears when a connection is configured). Agents carry no standing Bing tool;
it is attached to the individual runs that ask for it, and a Bing tool left on an agent by an earlier version
is removed at startup. Requesting it without `BING_CONNECTION_ID` returns `400`.

Microsoft documents that with Grounding with Bing, customer data (the search query and tool parameters) flows
to Bing, **outside the Azure compliance boundary** and under different data-processing terms. Don't enable it
for confidential RFPs without checking that those terms are acceptable. Only the generated search query is
sent; end-user identity isn't.

### Prompt-injection defences

RFPs, KB documents, user context and questions are third-party text, so they are treated as data:

- Each piece is wrapped in a fence with a random per-request marker (`<<<KB-SOURCE:3fa9…` … `KB-SOURCE:3fa9…>>>`;
  see `app/agents/untrusted.py`), and the agent instructions say that fenced text is never to be obeyed. Look-alike
  markers inside the text are defused.
- Model output is validated rather than trusted (`app/agents/normalize.py`): compliance must be Yes/Partial/No
  (anything else becomes Partial, never an unearned Yes), lists and evidence are coerced to the documented
  shape, section classifications may only use real sections and real requirement IDs, and citations are limited
  to the sources supplied (see [Retrieval and citations](#retrieval-and-citations)).
- `tone` is restricted to a short phrase of letters, spaces and hyphens.

These reduce the risk; they don't remove it. A model can still be influenced by hostile text, which is why the
compliance matrix and draft need human review.

---

## Project Structure

```
rfp-draft-agent/
├── app/
│   ├── main.py                    # FastAPI entry point
│   ├── config.py                  # Settings from env vars
│   ├── auth/
│   │   ├── entra.py               # Authentication (disabled / Easy Auth headers / Entra JWT)
│   │   ├── access.py              # Ownership, sharing and admin rules
│   │   └── ratelimit.py           # Per-user request limiter
│   ├── validation.py              # Checks for ids, file names and blob names
│   ├── agents/
│   │   ├── foundry_agent.py       # Foundry agent orchestration
│   │   ├── kb_search.py           # Hybrid KB search, query embedding, citation checks
│   │   ├── normalize.py           # Shape checks on model output
│   │   ├── prompts.py             # System instructions + JSON schemas
│   │   ├── retrieval.py           # DLAC + segmentation query builder
│   │   └── untrusted.py           # Fences that mark third-party text in prompts
│   ├── evaluation.py              # Retrieval metrics (hit rate, recall@k, MRR)
│   ├── ingestion/
│   │   ├── rfp_extract.py         # PDF/DOCX parsing + requirement extraction
│   │   ├── kb_ingest.py           # Blob → chunk → embed → index
│   │   ├── pipeline.py            # The one redaction path for uploads and request bodies
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
│   ├── setup_auth.py              # Create the Entra app registration for Easy Auth (azd)
│   ├── eval_retrieval.py          # Compare keyword / hybrid / semantic retrieval on your KB
│   └── demo.py                    # End-to-end demo script
├── evals/
│   └── retrieval_cases.example.jsonl   # Example evaluation cases (adapt to your KB)
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
