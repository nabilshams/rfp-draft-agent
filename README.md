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
| **RFP Ingestion** | PDF (PyPDF2), DOCX (python-docx, including tables) and spreadsheet parsing; extracts sections, shall/must requirements, questions (drafted like requirements), evaluation criteria, instructions, dates and submission rules, each typed (kind, response type, evidence required) with a source anchor and stable ID; repeated questions are grouped; an optional model-based reader covers the whole document |
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
pip install -r requirements.txt   # the pinned lock file (see "Dependencies" below)
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
| `MODEL_INPUT_PRICE_PER_MILLION` / `MODEL_OUTPUT_PRICE_PER_MILLION` | USD per million input / output tokens, used only for the cost estimate in telemetry (defaults `2.5` / `10.0`) |
| `AZURE_AI_SEARCH_CONNECTION_NAME` | Search service connection name/ID |
| `AI_SEARCH_INDEX_NAME` | Search index name (default: `rfp-knowledge-base`) |
| `AZURE_STORAGE_ACCOUNT_URL` | Blob storage account URL |
| `STORAGE_KB_CONTAINER` | Container for knowledge base docs (default: `knowledge-base`) |
| `STORAGE_RFP_CONTAINER` | Container for RFP uploads (default: `rfp-uploads`) |
| `STORAGE_OUTPUT_CONTAINER` | Container for generated outputs (default: `outputs`) |
| `STORAGE_JOBS_CONTAINER` | Container for background draft jobs (default: `draft-jobs`) |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | App Insights connection string |
| `AUTH_MODE` | `disabled`, `easyauth` or `jwt` (default `jwt`). `disabled` turns authentication off and logs a warning at startup; use it for local development only. See [Authentication](#authentication) |
| `ENTRA_TENANT_ID` | Entra ID tenant ID (`jwt` mode). The old `dev` value is no longer supported; use `AUTH_MODE=disabled` |
| `ENTRA_CLIENT_ID` | Entra ID client/app ID (`jwt` mode) |
| `ENTRA_AUDIENCE` | JWT audience (`jwt` mode; defaults to client ID) |
| `EMBEDDING_MODEL_DEPLOYMENT_NAME` | Embedding model (default: `text-embedding-3-large`) |
| `PARALLELISM` | Number of Uvicorn worker processes when running via `python -m app.main` (default: `1`) |
| `SECTION_PARALLELISM` | Number of sections synthesized concurrently in `per_section` and map-reduce phase 2 (default: `1`) |
| `RUN_TIMEOUT_SECONDS` | Longest a single agent run may take before it is cancelled and reported as `timed_out` (default: `600`) |
| `JOB_WORKERS` | Background drafts run at once in this process (default: `2`) |
| `JOB_MAX_ACTIVE_PER_USER` | Unfinished background drafts one user may have (default: `3`) |
| `JOB_HEARTBEAT_SECONDS` / `JOB_STALE_SECONDS` | How often a running job records that it is alive (default `15`), and how long without that before it is reported as interrupted (default `120`) |
| `VERIFY_ANSWERS` | Check every drafted answer's claims against the evidence it cites (see [Claim verification](#claim-verification)); roughly one extra model call per requirement. `true` by default |
| `VERIFIER_MODEL_DEPLOYMENT_NAME` | The model deployment the verifier runs on. Empty = the same as the writer (`FOUNDRY_MODEL_DEPLOYMENT_NAME`). A different deployment reduces errors the two would share; it must already exist in the Foundry account |
| `VERIFIER_INPUT_PRICE_PER_MILLION` / `VERIFIER_OUTPUT_PRICE_PER_MILLION` | USD per million tokens, only when the verifier's deployment is priced differently from the writer's (`MODEL_*_PRICE_PER_MILLION`). Used for the cost estimate in telemetry; empty = the writer's prices |
| `CONFIDENCE_FRESH_DAYS` / `CONFIDENCE_STALE_DAYS` | Age of the sources a claim rests on: newer than the first is fresh (default `730`), older than it caps the band at Medium, older than the second (default `1825`) at Low |
| `CONFIDENCE_HIGH_SUPPORT` / `CONFIDENCE_LOW_SUPPORT` | Share of an answer's claims the verifier found supported: High needs at least the first (default `0.9`); below the second (default `0.6`) is Low |
| `CONFIDENCE_TOP_RANK` / `CONFIDENCE_MIN_RELATIVE_SCORE` | A cited source should rank within the top N matches (default `3`) and score at least this share of the best match (default `0.5`), or the band stops at Medium |
| `EXTRACTION_CHUNK_CHARS` / `EXTRACTION_OVERLAP_CHARS` / `EXTRACTION_MAX_CHUNKS` | How the model-based extractor reads an RFP: part size (default `6000`), how much each part repeats of the one before (`400`), and the most parts it will read (`40`; a longer document is refused, not cut) |
| `SEARCH_TOP_K` | Knowledge-base documents retrieved per requirement or question (default: `5`) |
| `SEARCH_MIN_SCORE` | Drop weaker matches: the semantic reranker score (0-4) when `SEARCH_SEMANTIC_RANKER` is on, otherwise the search score (default: `0`, keep everything, so a requirement is only treated as having no evidence when the search returns nothing; the model and the citation check catch the rest) |
| `ROUTE_COMMERCIAL_TERMS` / `ROUTE_LEGAL_TERMS` | Requirements about pricing or contract terms are never drafted: they become a placeholder for the commercial or legal team (see [Answer states](#answer-states-and-never-invent)). Terms are separated by `;`; `price*` matches any word starting with `price`, other terms match whole words and phrases, and a requirement's category and section are read too. Defaults cover price, cost, fees, discounts, payment, quotes and budgets, and liability, indemnity, warranty, governing law, IP, insurance and termination. An empty list turns routing off |
| `SEARCH_SEMANTIC_RANKER` | Re-rank results with Azure AI Search's semantic ranker. A metered feature that needs the Basic tier or above; off by default (default: `false`) |
| `SEARCH_MAX_CONTEXT_DOCS` | Cap on documents shared by many requirements in one prompt: full drafts and section narratives (default: `15`) |
| `KB_TIER_WEIGHTS` | How much a source type moves a result up or down: `authoritative=1.3,approved_answer=1.15,historical=1.0,external=0.9`. The search score is multiplied by the weight; `1` changes nothing |
| `KB_STATUS_WEIGHTS` | Same for approval status: `approved=1.0,unreviewed=1.0,draft=0.8,superseded=0.5` |
| `KB_FRESHNESS_HALF_LIFE_DAYS` / `KB_FRESHNESS_FLOOR` | Age weighting: a document's weight falls from 1.0 towards the floor (default `0.7`), halfway after the half-life (default `730` days). `0` turns freshness off |
| `SEARCH_CANDIDATE_FACTOR` | Results are fetched this many times wider than `SEARCH_TOP_K` (default `3`), re-ordered by the weights, then cut to top-k |
| `COSMOS_ENDPOINT` / `COSMOS_DATABASE` / `COSMOS_CONTAINER` | The project ledger's Cosmos DB account, database (default `rfp-ledger`) and container (default `ledger`). Empty endpoint = ledger off |
| `LEDGER_BACKEND` | `memory` keeps the ledger in this process (local development only); empty uses Cosmos DB when `COSMOS_ENDPOINT` is set |
| `LEDGER_MAX_REQUIREMENTS` | Most requirements one project may hold (default `1000`) |
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
pytest tests/ -v --cov=app
```

CI fails if coverage of `app/` drops below the `fail_under` threshold in `pyproject.toml`. The UI tests run the
helpers in `app/static/safe.js` and the HTML builders in `app/static/render.js` with Node.js (escaping, project views)
and are skipped if it isn't installed.

### 6. Run the demo

```bash
python scripts/demo.py --base-url http://localhost:8000
```

---

## API Reference

### `GET /health`
Liveness probe (unauthenticated).

### `GET /health/dependencies`
Whether the app can reach storage, search and the agent service: `ok`, `refused` (missing role or firewall),
`unreachable` (DNS or network) or `error`. Requires authentication; reveals no details.

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
    { "id": "R1", "type": "requirement", "anchor": "¶2", "text": "The vendor shall ...", "category": "compliance", "priority": "mandatory", ... },
    { "id": "T-2", "type": "question", "anchor": "Table 1, row 3", "text": "How do you handle data residency?", ... }
  ],
  "questions": ["How do you handle data residency?"],
  "evaluation_criteria": [...],
  "response_instructions": {},
  "deadlines": [{ "text": "Responses must be submitted by 30 November 2026.", "date": "2026-11-30", "anchor": "¶14" }],
  "submission_rules": ["One electronic copy in PDF format shall be uploaded to the portal."],
  "method": "local"
}
```

- **Typed items.** Every item has `kind` (`question`, `mandatory`, `optional`, `instruction`, `evaluation_criterion`,
  `deadline` or `submission_rule`), `response_type` (`narrative`, `yes_no`, `table` or `attachment`),
  `evidence_required` (the RFP asks for a certificate, report, references...), `implicit` (inferred, not stated; only
  the model sets it) and `duplicate_of`. Only `question`, `mandatory` and `optional` are *answered*; an instruction
  ("each answer shall be no longer than 200 words"), a date or a submission rule is typed apart, and the last two are
  collected into `deadlines` and `submission_rules` for the project record. The kind comes from the item's wording
  (and a sheet's priority column: `S` is optional, `M` is mandatory; an item with no stated obligation is
  `mandatory`, and its `priority` stays `standard`).
- **Repeats.** An item whose content words are (nearly) the same as an earlier item's, with the same response type,
  has `duplicate_of` set to the first one's id, so it is answered once.
- A requirement is a whole sentence with its subject, not cut at a version number ("TLS 1.2").
- Questions are requirements of `type: "question"`: they are drafted like any other requirement. `questions` repeats
  their text for older clients.
- `anchor` says where an item is in the RFP: `p. 3` (PDF page), `¶12` (Word paragraph), `Table 2, row 5` (a Word
  table whose header has a "Requirement" or "Question" column, or any other table's text), or `Row 14` (spreadsheet
  row; only the active sheet is read).
- IDs are the RFP's own references when it has them (an ID column); otherwise `R1`, `R2`, ... in document order, so
  extracting a file again gives the same IDs. A repeated reference gets a suffix (`3.1`, `3.1-2`).
- A draft also reads the RFP's evaluation criteria and response instructions (length, format) from the file and
  gives them to the model as guidance on the *content* of each answer. A request cannot supply its own guidance.
- Everything in the result is redacted, including keywords and categories.

### `POST /rfp/extract-ai`
Read the RFP with the model instead of the patterns: better for documents that say what they want in prose. Same
request and response as `/rfp/extract`, with `"method": "ai"`, `coverage` (parts read, characters, items listed and
kept) and `warnings`.

- **All of the document is read.** The redacted text is cut into overlapping parts (`EXTRACTION_CHUNK_CHARS`, default
  6000, overlapping by `EXTRACTION_OVERLAP_CHARS`) and *every* part is read. A document needing more than
  `EXTRACTION_MAX_CHUNKS` (40) parts is refused with 413, never cut; a part the model cannot read fails the request
  with 502 after one retry, never leaving a hole. (The old version read the first 12,000 characters.)
- **Nothing is invented.** Each item carries a verbatim quote from the document, and an item whose quote is not found
  in it is dropped and counted in `warnings`. A stated item's text *is* its quote; an implicit item keeps the model's
  statement but must quote what implies it. The document's own ID for an item is kept only if the document contains it.
- **Typed, within lists.** Values outside the lists above are replaced by what the item's wording decides. Each item's
  `anchor` is found by locating its quote.
- Personal details are redacted before anything is sent (per line, so anchors survive), and the document is sent
  fenced as data, never as instructions.

On the golden RFPs the model finds every requirement and nothing else (recall and precision 100%), and agrees with the
labels on kind, response type and evidence flag for 86% of the items: it tends to call a "shall" statement `yes_no`
(`evals/baseline.md`). The projects API still uses the pattern-based extractor; the model-based one is for you to call
and review.

### `POST /draft`
Generate a full RFP response draft (DOCX + XLSX) in one request. Bound to a single request, so a large RFP can hit
the platform's 240-second limit; prefer [`POST /drafts`](#post-drafts) for those.

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

### `POST /drafts`
Start a draft in the background (the body is the same as `/draft/stream`, with optional `requirements`, `approach`,
`context` and `allow_web_search`). Returns `202` right away; poll the job for progress.

```bash
curl -X POST http://localhost:8000/drafts -H "Content-Type: application/json" \
  -d '{"rfp_id": "abc123", "client_id": "acme", "project_id": "proj-42", "approach": "per_requirement"}'
# {"job_id": "9f1c...", "status": "queued", "status_url": "/drafts/9f1c..."}

curl "http://localhost:8000/drafts/9f1c...?after=0"
# {"job_id": "9f1c...", "status": "running", "total": 12, "processed": 5,
#  "events": [{"type": "start", ...}, {"type": "response", "data": {...}}, ...], "next": 6,
#  "outputs": null, "error": ""}
```

Poll with `after` set to the previous reply's `next` to receive only new events, until `status` is `succeeded`
(`outputs` then holds the download URLs), `failed` (see `error`) or `cancelled`. `POST /drafts/{job_id}/cancel`
asks a running draft to stop. See [Background drafts](#background-drafts).

### Projects (the ledger)
A *project* is one bid, kept on the server so a page reload loses nothing and several people can work on it. It
holds the RFP's requirements (with the IDs and anchors from extraction), their status and assignee, **versioned
answers** and an **audit trail**. See [The project ledger](#the-project-ledger).

| Endpoint | What it does |
|---|---|
| `POST /projects` | `{ "name", "rfp_id" }` extracts the RFP's requirements into a new project (or send `"requirements": [...]` yourself). `"extractor": "ai"` has the model read every part of the document instead of the patterns (see [`POST /rfp/extract-ai`](#post-rfpextract-ai)). Optional `"share_with_groups": ["group-id"]`. Returns the whole project (201) |
| `GET /projects` | The projects you own or that are shared with one of your groups, newest first |
| `GET /projects/{id}` | The project, its requirements with their current answers, and counts by status. 404 if you can't open it |
| `PATCH /projects/{id}` | Rename, or change who it is shared with (owner or admin) |
| `DELETE /projects/{id}` | Delete it with all its answers and history (owner or admin) |
| `POST /projects/{id}/confirm-extraction` | Say you have reviewed the requirements the model extracted (see [Projects](#projects-the-ledger)); a project read by the model cannot be drafted until somebody has. Harmless on any other project |
| `PATCH /projects/{id}/requirements/{rid}` | Edit `text`, `category`, `priority`, `item_kind`, `response_type`, `evidence_required`, `duplicate_of` (`""` ungroups), `assignee`, `assignee_name` or `status` (`extracted`, `drafted`, `edited`, `approved`; `needs_input` is set by drafting, and an answer that is a placeholder, has conflicting evidence, or has a claim its evidence does not support cannot be approved, 409). Send `expected_rev` to be refused with 409 if somebody changed it since you looked |
| `GET` / `POST /projects/{id}/requirements/{rid}/answers` | List every version, newest first; add a version (`answer_markdown`, optional `meets`, `evidence`, `assumptions`, `risks`, `followups`) or make an earlier one current again (`{"restore_version": 2}`, recorded as a new version) |
| `GET /projects/{id}/calibration` | How each confidence band has fared with reviewers: per band, the model answers accepted as drafted, edited by a person, or still pending, and the edit rate (see [Confidence](#confidence-bands)) |
| `GET /projects/{id}/events` | The audit trail (who, what, when), newest first; `rid`, `limit` and `offset` narrow and page it |
| `POST /projects/{id}/draft` | Draft the project in the background (202). Body: `client_id`, `kb_project_id` (the knowledge-base project segment to search), optional `solution_area`, `tone`, `approach`, `context`, `allow_web_search`, and `rids` to redo specific requirements |
| `GET /projects/{id}/draft` | The latest draft: `job` (status, started_by, processed/total, `downloads`), `running`, `interrupted`, `remaining` (requirements still without an answer), `needs_input` (answers that are placeholders for a person), `failed`, `flagged` (answers with a claim the verifier found unsupported or contradicted) and `low_confidence` |
| `POST /projects/{id}/draft/cancel` | Ask the running draft to stop (whoever started it, the project owner or an admin). Answers already saved are kept |

**What the project holds about the RFP (#32).** Each requirement keeps its `item_kind` (the ledger's own `kind` says it
is a requirement document), `response_type`, `evidence_required`, `implicit` and `duplicate_of`; the project keeps the
RFP's `deadlines` (with ISO dates), `submission_rules` and `extraction` (`method`: `local`, `ai` or `supplied`;
`confirmed`; the model's `warnings` and `coverage`).
- **A draft works on the items that are answered** (`question`, `mandatory`, `optional`) **and are not repeats.** An
  instruction, evaluation criterion, date or submission rule is not drafted (instructions and criteria reach the drafting
  as guidance instead), and the draft status counts only what is left to draft.
- **A repeated question is answered once.** The first item is drafted; when its answer is saved each repeat gets the same
  answer as a new version (`answer.reused_from`), including its evidence, claims and confidence. A repeat a person has
  answered or approved keeps their answer; a repeat whose answer is a model's is replaced when the first is redrafted.
  Name a repeat in `rids` to draft it on its own, or ungroup it (`duplicate_of: ""`). Grouping is edited in the ledger:
  an item can only repeat an *answered* first item, never another repeat; moving an item moves its repeats with it, and
  an item that stops being answered releases them.
- **Review before drafting a model-read project.** A project created with `"extractor": "ai"` starts unconfirmed: edit
  anything the model got wrong (kind, response type, repeats, text), then `POST /projects/{id}/confirm-extraction`
  (recorded in the audit trail). Drafting it before that is a 409. Projects read by the patterns, or created from
  requirements you send, need no confirming. The page shows the dates, how to submit, the model's warnings and the
  confirm button above the requirements, a kind/response-type/evidence/"same as" tag on each item, and a form to change
  them.
- Not done: the writer is not yet told an item's response type (a `yes_no` item is still drafted as a narrative), and
  related-but-different questions are not grouped, only repeats.

**Drafting a project.** Each answer is written to the ledger as a new answer version the moment the model produces it
(source `model`, with the job id), so reloading the project shows answers appearing, and nothing is lost if the draft
is cancelled, fails, or the server restarts. Starting a draft again **picks up where it left off**: by default only
requirements without an answer are drafted, so finished work is never regenerated (an interrupted draft is reported
as `interrupted` by the status endpoint). An answer a person wrote or approved is not replaced unless that requirement
is named in `rids` (then the model's answer becomes a new version and the approval is withdrawn). If somebody edits a
requirement while the draft is running, the model's later answer for it is **not** saved over their edit. A
requirement whose generation failed gets no answer at all (no invented "No") and is marked `failed`, so the next run tries it again. Only one
draft runs per project at a time (409). The Word and Excel documents are built from **everything the project holds**,
not just this run's answers, and are published under the project id (see
[`GET /outputs`](#get-outputsrfp_idfilename)). Section approaches (`per_section`, `map_reduce`) save the per-requirement
answers; their section narratives stay in the job's events for now.

Rules: anyone who can open a project can read it and edit its requirements and answers; only the owner (or an
administrator) can rename, share or delete it. A project nobody shared with you is a `404`, not a `403`. Every edit is
attributed and timestamped. Text you write is redacted on arrival like all other input. Requirement IDs in the
ledger contain only letters, digits, `.`, `_` and `-` (anything else in the RFP's own reference becomes `-`).
Without a configured ledger these endpoints answer `503`.

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
  -F "share_with_groups=group-id-1,group-id-2" \   # optional; default: private
  -F "source_type=approved_answer" -F "effective_date=2026-01-15" -F "version=3" \
  -F "authority=Security team" -F "approval_status=approved"   # all optional; see below
```

Stored as `client_id/project_id/solution_area/filename`. Uploading the same name again replaces the document
and its sharing; only its owner (or an admin) can do that.

**Source types.** `source_type` says how far a document should be trusted: `authoritative` (current policy,
certifications, product facts; **only an administrator can set it**), `approved_answer` (a reviewed past answer),
`historical` (the default: old proposals, anything unreviewed) or `external`. `effective_date` (`YYYY-MM-DD`,
default today), `version`, `authority` (who vouches for it), `owner_name` and `approval_status` (`approved`,
`unreviewed` (default), `draft`, `superseded`) complete the provenance. It is stored on the blob, copied onto every
chunk in the search index, and a replaced document keeps whatever it had for fields you don't repeat.

### `POST /kb/ingest`
Re-ingest a KB document you own. Who can retrieve it comes from the sharing recorded on the document, never
from the request (sending `oids` or `groups` is rejected with 422). An admin ingesting a document that has no
recorded owner becomes its owner. The same provenance fields as on upload can be sent to change them without
uploading the file again (`{"blob_name": "...", "source_type": "approved_answer", "version": "4"}`).

```bash
curl -X POST http://localhost:8000/kb/ingest \
  -H "Content-Type: application/json" \
  -d '{ "blob_name": "acme/proj-42/cloud/solution_guide.pdf" }'
```

### `GET /kb/doc/{ref}`
Opens a knowledge-base document from an evidence link. Retrieval never gives the model (or the user) a blob URL:
those contain the customer/project folder names and do not open (the storage account is private). A source is
shown as `/kb/doc/<ref>`, where `<ref>` is a 24-character hash of the blob name. The app resolves it, checks that you
could see the document in the knowledge base (owner, a shared group, or administrator) and returns it: PDFs
open in the browser (the UI adds `#page=N` when the evidence has a page), Word files download. A reference that
doesn't exist and a document you can't see both return `404`. Links in the downloaded Word and Excel outputs are
the same relative paths, so they work from a signed-in browser on the app's address.

### `GET /kb/list`, `DELETE /kb/delete?blob_name=…`, `POST /kb/reindex`
`list` returns only documents you own or that are shared with one of your groups. `delete` is for the owner or
an admin (a document you can't see returns 404; one you can see but don't own returns 403). `reindex`
(admins only) re-indexes every document with the access recorded on it and skips documents that have no owner.

### `GET /outputs/{rfp_id}/{filename}`
Download a generated output file (`draft.docx` or `compliance.xlsx`) of an RFP you own, or of a **project** you can open
(a project's documents are published under its id, so group members who were never given the RFP can download them).

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
| Container Apps environment (in a VNet) + app (1 vCPU / 2 GiB, single replica) | Hosts the API and UI |
| VNet + private endpoint + private DNS zone | Private path to storage (see [Private storage](#private-storage)) |
| Container Registry (Basic) | Stores the image (pulled with managed identity) |
| AI Services account + Foundry project | Agents; `gpt-4o` and `text-embedding-3-small` deployments |
| AI Search (`basic`) | Knowledge-base index (created on first KB ingest) |
| Storage account | `knowledge-base`, `rfp-uploads`, `outputs` and `draft-jobs` containers |
| Log Analytics + Application Insights + alert rules | Logs, traces and alerts (see [Monitoring and alerts](#monitoring-and-alerts)) |
| User-assigned managed identity | Used by the app; has the data-plane roles below |

Keyless by design: storage, search and AI Services have local (key) auth disabled and the app uses
`DefaultAzureCredential`. The managed identity gets Storage Blob Data Contributor, Search Index Data
Contributor, Search Service Contributor, Azure AI User and Cognitive Services OpenAI User. The identity
running `azd` gets the same roles for Search and AI Services so you can run scripts against them; storage is
reachable only from inside the VNet (next section).

### Private storage

The storage account has **public network access disabled** and is reached through a **private endpoint** in a VNet
(`infra/modules/network.bicep`: VNet, private endpoint, `privatelink.blob.core.windows.net` DNS zone linked to the
VNet). The Container Apps environment is created inside that VNet (a workload-profiles environment, ingress still
external, so the app is reachable from the internet behind Easy Auth). Some subscriptions enforce this with a
governance policy that sets storage public access to `Disabled` and reverts any attempt to change it.

Consequences to know about:

- **Your machine can't reach the storage account.** Running the app locally against the deployed storage fails
  (use [Azurite](https://learn.microsoft.com/azure/storage/common/storage-use-azurite) via `docker-compose`, or a
  separate dev storage account that allows public access). The Azure portal's storage browser is blocked for the
  same reason.
- **The environment's network can't be changed in place.** Changing the VNet address ranges, or moving an existing
  deployment into the VNet, means deleting the Container Apps environment and app and provisioning again (about 20
  minutes). The app URL changes when that happens, so re-run `python scripts/setup_auth.py` (it only refreshes the
  redirect URI) and set `azd env set SERVICE_API_RESOURCE_EXISTS false` before provisioning.
- **Check it from the logs.** At startup the app logs `Dependency status: storage=ok, search=ok, agents=ok`;
  `refused` means a missing role or a firewall, `unreachable` means DNS or the private endpoint. Signed-in users can
  call `GET /health/dependencies` for the same answer. When Azure refuses the app, API calls return `503` with a
  plain message instead of a bare `500`.

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
| `ALERT_EMAIL` | _(empty)_ | Address that receives alert emails. See [Monitoring and alerts](#monitoring-and-alerts). |

> **Security:** with the default `AUTH_MODE=disabled` the deployed app has **no authentication** and is
> reachable from the internet. `azd provision` prints a warning when that is the case (and the app logs one at
> startup). Turn on [authentication](#authentication) or set `ALLOWED_IP_RANGES` before loading real RFP data.

### Dependencies

`requirements.in` lists the direct dependencies with minimum versions; `requirements.txt` is the **lock file**
generated from it (every package pinned, for Python 3.11, the version used by the Dockerfile and CI). The image
and CI install the lock file, so builds are repeatable. To update it:

```bash
uv pip compile requirements.in --python-version 3.11 --universal -o requirements.txt --no-header
pytest tests/        # run the suite against the new versions before committing
```

Dependabot proposes weekly updates (grouped for the Azure SDKs and OpenTelemetry).

### Continuous deployment

`.github/workflows/deploy.yml` deploys on demand (**Actions → Deploy → Run workflow**): it runs lint and tests, then
`azd deploy` (and `azd provision` when you tick the box) using OIDC federation, so no credentials are stored in
GitHub. It is manual on purpose, because a deployment changes billable resources. One-time setup:

```bash
azd pipeline config --provider github     # creates the federated credential and the repository variables
```

(or create a service principal with a federated credential for this repository and set the variables listed at the
top of the workflow). Add `ENTRA_CLIENT_SECRET` as a repository **secret** when `AUTH_MODE=easyauth`, and put
required reviewers on the `production` environment in the repository settings. The workflow has not been run from
GitHub yet; the same `azd` commands were run from a workstation.

### Monitoring and alerts

Alert rules are created with the infrastructure (`infra/modules/alerts.bicep`): server errors on real requests
(5 or more in 15 minutes, health probes excluded), unhandled exceptions (10 or more in 15 minutes) and container
restarts (more than 2 in 15 minutes, which is what a crash loop from bad configuration looks like). Set
`azd env set ALERT_EMAIL you@example.com` before `azd provision` to be emailed; without it the alerts still fire and
appear in the portal. Logs and traces go to Application Insights and Log Analytics (`ContainerAppConsoleLogs_CL`).

**What each draft records.** A draft is one trace. Its root `draft` span carries the job id, approach, requirements
answered, model calls, input and output tokens and an **estimated cost in USD** (tokens times
`MODEL_INPUT_PRICE_PER_MILLION` / `MODEL_OUTPUT_PRICE_PER_MILLION`, which default to GPT-4o list prices: set your own;
this is an estimate, not an Azure bill). Under it, a `requirement` span per requirement holds its stages: `kb.search`
(results, mode), `llm.run` (model, status, latency, tokens, cost) and the count of unsupported figures removed. Section
approaches add `section.classify` and `section.narrative`, and storing the documents is `outputs`. They show up in
Application Insights as dependencies. The ready-made queries are in [`infra/queries/`](infra/queries):
`draft_cost.kql` (one row per draft with its cost), `draft_trace.kql` (every stage of one job in order) and
`stage_latency.kql` (where time and money go). A draft that is cancelled or fails still reports what it spent.

**No content in telemetry.** A span can only carry attributes from a fixed list (identifiers, counts, durations,
tokens, outcomes) and only numbers, booleans or short names. There is no way to attach a prompt, an answer, a
requirement's text or an exception message, and a test sends secret text through a whole draft and checks it
appears in no span. Exceptions are recorded by type only.

### Cost and quota

Rough monthly cost of the defaults in `australiaeast`, idle (check the [Azure pricing calculator](https://azure.microsoft.com/pricing/calculator/) for current figures):

| Resource | Notes |
|---|---|
| AI Search `basic` | The largest fixed cost: billed per hour whether or not it is used. Use `searchSku` and `azd down` for throwaway environments. |
| Container Apps (1 vCPU / 2 GiB, 1 replica always on) | `minReplicas: 1` keeps the live log stream and background drafts alive; it is billed while running. The VNet-integrated environment uses the Consumption workload profile. |
| Private endpoint (storage) | A small hourly fee plus per-GB data processing; the VNet and the private DNS zone are close to free. |
| Container Registry `Basic` | Small fixed cost. |
| Log Analytics / Application Insights | Pay per GB ingested; 30-day retention. |
| Alert rules | A small per-rule monthly fee for the log alerts. |
| `gpt-4o` (GlobalStandard, 30K TPM) and `text-embedding-3-small` (50K TPM) | Pay per token. The capacity is quota, not spend, but the region must have it available. |

Web search through Bing grounding, the semantic ranker (`SEARCH_SEMANTIC_RANKER`) and each draft's model calls are
metered separately. A draft makes about one model call per requirement, plus a few for sections.

### The project ledger

**In the web UI.** On the *RFP Response* tab, "Your projects" lists the projects you own or that are shared with
your groups. Upload an RFP and press **Create project** (give it a name, and optionally group IDs to share it with):
its requirements are extracted into a project that lives on the server. Each requirement shows its status (*No answer
yet*, *Drafted*, *Edited*, *Approved*), anchor, and answer with its version, author and time. You can **write or edit
an answer** (saved as a new version; the earlier one is kept), **approve** it, see all **versions** and **restore**
one, read a requirement's **history** or the project's **recent activity**, **draft** the unanswered requirements
(answers appear as they are saved; you can leave, cancel, or draft again to carry on) or **redraft** one requirement.
The address bar names the open project (`#project=<id>`), so reloading, or sending the link to a colleague who has
access, shows the same state. The page refreshes itself every 15 seconds and never overwrites an answer you are
typing; saving over a colleague's newer change is refused with a message and their version is shown. If the server has
no ledger, the page works as before with its state in the browser.

Projects, requirements, answer versions and the audit trail are stored in **Azure Cosmos DB for NoSQL** (serverless),
created by `infra/modules/cosmos.bicep`:

- **Serverless:** no provisioned throughput. An idle ledger costs almost nothing; you pay per request and for storage.
- **Entra ID only:** local (key) authentication is disabled. The app's managed identity has the built-in *Cosmos DB
  Built-in Data Contributor* data-plane role. There are no keys to leak.
- **Private:** public network access is disabled; the app reaches it through a private endpoint and the
  `privatelink.documents.azure.com` DNS zone in the same VNet as storage (`modules/network.bicep`). You cannot connect
  from your own machine.
- **One container** (`ledger`, partition key `/projectId`) holds a whole project, so a change to a requirement, the
  new answer version and its audit event are written in a single atomic transactional batch, and a stale edit is
  refused by the document's etag. A project holds up to `LEDGER_MAX_REQUIREMENTS` (default 1000) requirements.
- **Alert:** more than 5 throttled or failed (429/500/503) requests in 15 minutes.

The app finds it through `COSMOS_ENDPOINT`, `COSMOS_DATABASE` and `COSMOS_CONTAINER` (set by the infrastructure);
`/health/dependencies` and the startup log report it as `ledger`. For local development without Azure set
`LEDGER_BACKEND=memory` (data lives in the process and is lost on restart). After a deployment you can check the real
database from inside the container:

```bash
az containerapp exec -n <app> -g <resource-group> --command "python -m app.ledger.smoke"
```

### Not covered yet

- **Single replica.** The live log stream, `/metrics` counters and the per-user rate limiter are held in process
  memory, so the app runs one replica. Background-draft state is already in Blob Storage; move the rest out of
  process (and the in-process job workers to a queue-triggered worker) before scaling out.
- **Private networking for Search and AI Services.** Storage is private (see [Private storage](#private-storage));
  Search and AI Services still have public endpoints, protected by Entra ID and RBAC with key auth disabled. Add
  private endpoints for them if your policy requires it (tracked in #25).
- **Grounding with Bing.** The connection is created by hand in the Foundry portal (Management center → Connected
  resources); put its resource ID in `BING_CONNECTION_ID` (`scripts/list_connections.py` lists it). Read
  [Web search](#web-search-bing-grounding) about the data flow before enabling it.

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
  A draft over a large RFP can take longer, so the UI and any client that drafts large RFPs should use
  [`POST /drafts`](#post-drafts), which runs the draft in the background. `POST /draft` and `POST /draft/stream`
  stay bound to one request and can be cut off by the platform (whether a long stream is also subject to the
  limit has not been tested).

### Background drafts

`POST /drafts` takes the same body as `/draft/stream`, checks it (RFP access, web-search setting), stores a job and
returns `202` with a `job_id` straight away. The draft runs on a worker thread (`JOB_WORKERS` at a time per
process; at most `JOB_MAX_ACTIVE_PER_USER` unfinished jobs per user, else `429`).

- **State is in Blob Storage** (`draft-jobs` container, one JSON blob per job), not in memory, so any replica can
  answer a poll. The record holds the status, progress and every event, the same events `/draft/stream` sends.
- **A restart loses the work in flight.** The worker records a heartbeat every `JOB_HEARTBEAT_SECONDS`; a job that
  is still `queued`/`running` with no heartbeat for `JOB_STALE_SECONDS` is reported as `failed` ("interrupted")
  when someone asks for it, and the user starts it again. A plain `/drafts` job resumes nothing; a **project** draft
  (`POST /projects/{id}/draft`) already has every finished answer in the ledger, so starting it again only does what
  is left.
- **Cancel** with `POST /drafts/{job_id}/cancel`; the worker stops between steps (a step is one agent call, so it
  can take a moment).
- **Access** is the owner or an administrator; anyone else gets `404`.
- Job blobs are deleted after 7 days by a storage lifecycle rule created with the infrastructure
  (`infra/resources.bicep`).
- The documents are stored exactly as for `/draft`: download them from `/outputs/{rfp_id}/draft.docx` and
  `compliance.xlsx` when the job succeeds.

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

**Current, authoritative sources rank first.** Every chunk carries its document's provenance (source type,
approval status, effective date, version) and where it came from (section heading, and page for PDFs). After
retrieval the search score is multiplied by a weight for the source type, approval status and age (see `KB_*`
settings), so a current policy outranks an older proposal that happens to match the wording better. Nothing is
hidden: lower-ranked sources still appear, after the better ones. The prompt labels each source
(`Source: authoritative, approved, effective 2026-09-01, version 4, section "3. Encryption", page 7`) and tells the
model to prefer the more authoritative and recent source and to record any disagreement under `risks`. Evidence
entries in a response carry the same fields (`source_type`, `effective_date`, `version`, `section`, `page`), and
the UI, the Word document and the compliance matrix show them.

**Evidence links are references, not blob URLs.** Knowledge-base sources reach the prompt, the answer and the
evidence as `/kb/doc/<ref>` (see [`GET /kb/doc/{ref}`](#get-kbdocref)), so a customer's folder name never appears
in what the model sees and every link goes through sign-in and the document's access check. The model may cite
only the references it was given; a link to anything else (including a raw blob URL) is removed. Customer
identity stays where access control uses it (the blob path and the index's `client_id` and `oids`/`groups`
fields), not in the text of the prompt. A file's name is still shown as the source title (and is redacted like
any other text), so name files without customer names if that matters to you.

**Adding provenance to existing documents.** The index gains the new fields automatically when the app starts
(an additive change; no downtime). Chunks indexed earlier have no values until the document is re-indexed:
an administrator runs `POST /kb/reindex` (or "Re-index All" in the UI). Documents that never had provenance are
treated as `historical` and `unreviewed`, dated by when the file was last changed, so nothing is promoted by
accident; change one with `POST /kb/ingest` and the provenance fields.

<a id="answer-states-and-never-invent"></a>
**Answer states: when the sources are not enough, nothing is invented.** `meets` (Yes/Partial/No) is the compliance
claim; the *state* says how far the answer can be trusted and who has to act. Each response, and each ledger
requirement, carries one:

| State | Meaning |
|---|---|
| `fully_answered` / `partially_answered` | Grounded in retrieved sources, cited; a partial answer says what is missing (`missing`) |
| `conflicting_evidence` | The sources disagree; the draft follows the more authoritative one and a person decides |
| `no_evidence` | Nothing in the knowledge base covers it. The answer text is a placeholder, `[SME INPUT REQUIRED: <what is missing>]` |
| `needs_sme` | An expert has to supply or confirm it: the same placeholder |
| `needs_commercial` / `needs_legal` | Pricing and contract terms: never drafted, always `[COMMERCIAL INPUT REQUIRED: …]` / `[LEGAL INPUT REQUIRED: …]` |
| `failed` | The draft could not be generated (an error, a timeout, an unreadable reply). Recorded as a failure, not as "No" |

The rule is enforced in code, not only in the prompt. A requirement is **not sent to the model** when it is routed to
the commercial or legal team (`ROUTE_*_TERMS`), or when nothing was retrieved for it and web search is off. Otherwise
the model reports `sufficiency` and `missing`, and the app checks it: an answer that cites nothing whose quote
really appears in a retrieved source, or that opens by saying the sources don't cover it, becomes a placeholder and
**the model's prose is discarded**. A placeholder makes no compliance claim (`meets` is empty, shown as "Not
assessed"), the requirement's status becomes `needs_input`, and it cannot be approved until a person writes the
answer (which is then `fully_answered`). Section narratives leave placeholders out. The Word document shows each
requirement's status and what is missing, and the compliance matrix has an "Answer state" column (last, so existing
columns keep their positions). Whether a *cited* source really supports each claim is judged claim by claim by the
verifier planned in #35; these states are the floor underneath it.

<a id="claim-verification"></a>
**Claim verification: a second, independent check.** The writer does not just report its own confidence. Beside the
answer it lists the **claims** the answer makes (each a short factual statement) and the **evidence ids** that support
each one. Then, independently of the writer:

1. *Deterministic checks first, with no model.* A claim that cites nothing, whose cited quote is not really in the
   retrieved source (a fabricated or paraphrased quote), or that states a percentage, amount or count that the cited
   text does not contain, is `unsupported` immediately.
2. *A separate verifier* (its own agent and prompt, `rfp-verifier-agent`, optionally on its own model deployment:
   `VERIFIER_MODEL_DEPLOYMENT_NAME`) judges the rest against the passages they cite, and only those: `supported`,
   `partial`, `unsupported` or `contradicted`. Names the claim uses that its evidence never mentions are pointed out to it.

Each answer carries `claims` (text, evidence ids, verdict, reason, whether a rule or the verifier decided) and a
`verification` summary: `verified` (all claims supported), `partially_verified`, `flagged` (an unsupported or
contradicted claim) or `unchecked` (no claims listed, verification off, or the verifier could not be reached: a verifier
outage never fails a draft). Evidence entries get ids and a `quote_verified` flag. A project stores every claim with its evidence ids, verdict, reason
and whether a rule or the verifier decided, with each answer version (and so in its history); the requirement carries the
status, and **an answer whose status is `flagged` cannot be approved** (409). Writing the answer yourself clears it (the
text is then yours, and nothing in it was machine-checked), and restoring an earlier version brings its claims and status
back. The audit trail records the status, never the claims. The Word document and the compliance matrix show each
answer's verification (the matrix's last column, with flagged claims in the notes), and the page shows a badge, the
claims (flagged ones open, with the reason and the evidence they cite) and a warning on any quote that was not found in
its source. Nothing is rewritten or removed: a
contradicted claim makes the answer `conflicting_evidence`, an unsupported one downgrades `fully_answered` to
`partially_answered`, and every flagged claim is listed under the answer's risks, so the Word document, the matrix and
the UI show it. Placeholders and failures have nothing to verify. A claim that rests only on a web source can't be
checked (its page isn't visible here) and is left `unchecked`.

On the golden set, 32 claims written against the knowledge base (16 deliberately wrong: fabricated quotes, an invented
capability, a changed number, an overstated scope; 16 correct, including paraphrases) are checked by the real code.
The baseline catches all 16 wrong ones, rejects none of the correct ones, and catches 6 by rules alone, with no
model call (four are built to be rule cases; two changed numbers happen to be too). Targets: at least 80% caught, at most 10% of correct claims rejected. The writer's own claims on the golden RFPs
come out 88% supported: the verifier flags real overreach (for example "sessions are conducted live" where the source
says only "online"). Verification adds about 0.6k input and 0.1k output tokens and a few seconds per requirement
(`evals/baseline.md`). Whether the verifier agrees with a person is not measured here: it judges only the passage a claim
cites, so a claim that is true but cited to the wrong passage is flagged, and a superseded source that the answer cites
correctly is not (finding conflicts between sources is a separate piece of work, #41).

<a id="confidence-bands"></a>
**Confidence: a band, with the reasons.** Every drafted answer carries `confidence`: a band (**High**, **Medium** or
**Low**) and the list of reasons that produced it, shown beside the answer, in the requirement's header (so a reviewer
scanning the list sees Low first), in the Word document and in the matrix's last column. There is no percentage, and
nothing the model says about itself is an input. Each input is a fact the system can check:

- how many *independent* sources (distinct documents) have a quote that is really in the document they cite;
- how authoritative they are: an `authoritative` source, or an `approved` past answer, can stand alone; a historical,
  external, unreviewed, superseded or draft one needs a second source behind the same claim;
- how old they are, against `CONFIDENCE_FRESH_DAYS` and `CONFIDENCE_STALE_DAYS`;
- how well the cited sources matched the requirement: their rank among the retrieved documents and their score
  relative to the best match;
- the share of the answer's claims the verifier found supported (`CONFIDENCE_HIGH_SUPPORT`, `CONFIDENCE_LOW_SUPPORT`);
- whether the sources conflict, and whether only part of the requirement is covered.

These are judged **claim by claim**: an answer is only as strong as its weakest claim, so a claim that rests only on a
five-year-old proposal lowers the band even when another claim cites a current policy. Each rule that applies adds a
reason that lowers the band to Medium or Low, or one that supports it; the band is the lowest any reason allows, so
changing one input (removing a source, an older date) changes the band in a way you can read off the reasons. Claims
that were not checked (verification off, a web-only source, or a verifier outage) cap it at Medium; the share of supported claims is taken over the claims that were judged. Placeholders and
failures are Low (there is no answer to trust); text a person wrote has no band. Similarity to an approved answer
becomes an input when the answer library exists (#38). Thresholds are configuration, never code.

**Calibration.** The band earns trust by how it fares with people. `GET /projects/{id}/calibration` reports, per
band, the model's latest answers that were approved as drafted, edited by a person, or are still pending, and the edit
rate: a calibrated band has a falling edit rate as it rises. On the golden set (`evals/baseline.md`) the evaluation
reports the same split, using what a person would have to change (a wrong fact, a wrong compliance value, an invented
figure, answering what it should not) and never the verifier's verdicts, which the band is made from. On the 32
injected answers of the verifier's evaluation (16 deliberately wrong, 16 right, decided in advance) every High-band
answer is right and every Low-band answer is wrong: 0% against 100%. That shows the band follows the evidence and the
verifier's verdicts; it is also only as good as the verifier, and a replay with a verifier that approves everything
drops it sharply. The model's own recorded answers on the three golden RFPs contain no answer that fails the golden
checks (the verifier's finer flags are the band's input, so they cannot also be its label), so they cannot
calibrate it and their edit rates are all 0. How well the bands are tuned for your content needs real review outcomes,
which the endpoint collects as your team works. The evaluation's clock is pinned (`AS_OF`) so source ages do not drift
under the committed baseline.

**Invented figures are removed.** The prompts forbid made-up statistics, customer results and experience claims, and
a check enforces it: every percentage, amount, multiplier and count of customers, projects or years in a generated
answer or section narrative must also appear in what the model was given (the retrieved sources, the requirement,
the common context and the RFP's guidance). A sentence with any other figure is replaced by
`[Statement removed: it contained figures that are not in the sources – recommend human review.]`, and the answer
lists the removed figures under its risks. This catches invented numbers only; whether a claim without numbers is
true still needs a person (or the verifier planned in #35). With web search on, figures in the model's own output
are trusted, because the pages it read can't be seen.

**Evaluating quality.** `python scripts/eval_pipeline.py` scores the pipeline against a small *golden set*
(`evals/golden/`): three fictional RFPs (Word with a requirements table, a spreadsheet, a PDF), a fixture knowledge
base that includes a current authoritative policy and a stale conflicting proposal, and labelled requirements,
some of which the knowledge base cannot answer (bait for invented claims). Extraction, retrieval and its ranking,
the prompts, citation checks and the removal of invented figures are the real code; only the model is replayed from
recorded answers, so the command needs no Azure access and gives the same result every time. It reports extraction
recall and precision, retrieval hit rate and MRR, citation support (is the quote really in the cited document),
unsupported figures (written by the model, and delivered after the app removes them), abstention correctness
(declines when it should, answers when it can), answer and compliance correctness, and, from the live recording,
latency and tokens per requirement (`--input-price`/`--output-price` add a cost estimate). The report is written to
`evals/reports/` (JSON and Markdown); the committed baseline is [`evals/baseline.md`](evals/baseline.md).

- `python scripts/eval_pipeline.py --check` exits 1 if any metric is worse than `evals/baseline.json` by more than
  its tolerance (2 points). The test suite runs the same check (`tests/test_eval_harness.py`), so CI fails on a
  regression in extraction, retrieval, prompts' post-processing or scoring.
- `--mode live --record` calls the real model (billable; about 65 calls: the writer, the verifier on its answers, and the verifier on the injected claims) and saves its answers under
  `evals/golden/recordings/<variant>/`. Do this when the prompts or the model change on purpose, then
  `--write-baseline` to accept the new numbers. `--mode live --degrade --record` adds a deliberately bad
  instruction; the recorded `degraded` variant is kept to show that the metrics notice and that the guardrails
  hold (the model's invented figures are removed, and answers that cite nothing become placeholders).
- Answer states (#33) took the false-answer rate on the unsupported requirements from 60% to 0%: each now ends as
  a placeholder for a person (`state_correctness` checks this, and the golden set includes a pricing and a legal
  requirement that must be routed, not drafted). The baseline still shows an honest gap: the model quotes a stale
  figure ("99.5%") while calling it outdated (answer correctness 94%). Whether a cited source supports a claim is the
  verifier's job (#35).
- Extraction (#32) is scored per file format (recall and precision for PDF, Word and spreadsheet, held to 95% and 90%),
  plus typed-field accuracy, repeated questions grouped correctly, and instructions, deadlines and submission rules
  found and typed. The golden Word RFP now has a submission section and the spreadsheet a repeated question. The
  "each answer shall be no longer than 200 words" false positive of the first baseline is gone: it is typed as an
  instruction, not drafted. The model-based extractor is recorded once (`--mode live --extraction-only --record`, 3
  calls) and scored on the same RFPs.
- Add cases by adding a JSON file under `evals/golden/rfps/` (and documents to `kb.json`), then record. Keep the
  fixtures fictional: a test fails if they contain contact details, URLs or anything the redaction rules flag.

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
4. **Rotate and refresh** – give every document a real `effective_date` and `version`, mark replaced material
   `superseded`, and periodically remove stale content. Old documents are ranked lower, not hidden
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

### Web UI safety

RFP content, model output and server messages are untrusted, so the UI never inserts them into the page as markup:
text goes in with `textContent`, or through `esc()` (escapes `& < > " '`) when an HTML string is built, and links
built from model output go through `safeUrl()` (only `http`/`https`). There are no inline scripts or event
handlers, and the UI is served with a Content-Security-Policy that only allows scripts from `/static`
(`script-src 'self'`), plus `X-Frame-Options: DENY` and `X-Content-Type-Options: nosniff`. `tests/test_ui_safety.py`
checks the helpers, the page structure and the headers.

---

## Project Structure

```
rfp-draft-agent/
├── app/
│   ├── main.py                    # App assembly: lifespan, routers, security headers, UI
│   ├── config.py                  # Settings from env vars (incl. derived endpoints)
│   ├── azure_clients.py           # Shared, cached Azure SDK clients (blob, search, OpenAI, agents)
│   ├── schemas.py                 # Request bodies
│   ├── health.py                  # Dependency checks (startup log and /health/dependencies)
│   ├── logging_setup.py           # Console logging + live log stream
│   ├── metrics.py                 # Request counters for /metrics
│   ├── routers/                   # One module per area: ops, rfp, draft, jobs, projects, kb, outputs, template
│   ├── ledger/                    # Project ledger: store (Cosmos DB / memory), service, drafting, schemas, smoke check
│   ├── services/                  # Shared steps: RFP loading/publishing, draft pipeline, background jobs, KB access, uploads
│   ├── static/                    # Web UI: index.html, app.css, app.js, project.js (projects), render.js (HTML builders), safe.js (escaping helpers)
│   ├── auth/
│   │   ├── entra.py               # Authentication (disabled / Easy Auth headers / Entra JWT)
│   │   ├── access.py              # Ownership, sharing and admin rules
│   │   └── ratelimit.py           # Per-user request limiter
│   ├── validation.py              # Checks for ids, file names and blob names
│   ├── agents/
│   │   ├── lifecycle.py           # Persistent Foundry agents: find, create, update
│   │   ├── runner.py              # Running a prompt on an agent; output guard and JSON parsing
│   │   ├── answers.py             # Full draft, single requirement, single question
│   │   ├── sections.py            # Section classification and narratives
│   │   ├── strategies.py          # Per-requirement, per-section and map-reduce streaming
│   │   ├── kb_search.py           # Hybrid KB search, query embedding, citation checks
│   │   ├── grounding.py           # Removes figures that are not in the sources
│   │   ├── normalize.py           # Shape checks on model output
│   │   ├── states.py              # Answer states, routing to people, and the "never invent" rule
│   │   ├── verify.py              # Claim verification: rules first, then an independent verifier
│   │   ├── confidence.py          # The explainable confidence band (High/Medium/Low) and its reasons
│   │   ├── prompts.py             # System instructions + JSON schemas
│   │   ├── retrieval.py           # DLAC + segmentation query builder
│   │   └── untrusted.py           # Fences that mark third-party text in prompts
│   ├── kb_refs.py                 # Opaque /kb/doc/<ref> links for knowledge-base documents
│   ├── evaluation.py              # Retrieval metrics (hit rate, recall@k, MRR)
│   ├── evalkit/                   # Golden-set evaluation: fixtures, scoring, replay/live runs, reports
│   ├── ingestion/
│   │   ├── rfp_extract.py         # PDF/DOCX parsing + requirement extraction
│   │   ├── classify.py            # Kind, response type, evidence flag, dates and repeated questions
│   │   ├── ai_extract.py          # Model-based extraction: every part read, quotes verified
│   │   ├── kb_ingest.py           # Blob → chunk (section, page) → embed → index
│   │   ├── provenance.py          # Source types, dates, versions and the ranking weights
│   │   ├── pipeline.py            # The one redaction path for uploads and request bodies
│   │   └── redaction.py           # Regex-based redaction engine
│   ├── outputs/
│   │   ├── docx_writer.py         # DOCX generation
│   │   └── xlsx_writer.py         # XLSX compliance matrix
│   └── telemetry/
│       ├── tracing.py             # OpenTelemetry + App Insights setup
│       └── spans.py               # Stage spans, tokens and cost, no-content rule
├── config/
│   └── redaction_policy.yml       # Redaction rules (YAML)
├── infra/
│   ├── main.bicep                 # azd entry point (subscription scope, creates the resource group)
│   ├── resources.bicep            # Foundry, Search, Storage, ACR, Container Apps, RBAC
│   ├── modules/alerts.bicep       # Alert rules (failed requests, exceptions, restarts)
│   ├── modules/network.bicep      # VNet, private endpoints (storage, Cosmos DB) and private DNS
│   ├── modules/cosmos.bicep       # Cosmos DB account (serverless, private, Entra-only), database, container, role
│   └── main.parameters.json       # Maps azd environment values to Bicep parameters
├── azure.yaml                     # azd service definition (Container App, remote build)
├── templates/
│   └── rfp_response_template.docx # DOCX template (generate with scripts/create_template.py)
├── scripts/
│   ├── create_template.py         # Generate the DOCX template
│   ├── setup_auth.py              # Create the Entra app registration for Easy Auth (azd)
│   ├── check_exposure.py          # azd post-provision hook: warns about open, unauthenticated deployments
│   ├── eval_retrieval.py          # Compare keyword / hybrid / semantic retrieval on your KB
│   ├── eval_pipeline.py           # Score the whole pipeline on the golden set (replay or live)
│   ├── list_connections.py        # List Foundry project connections (find the BING_CONNECTION_ID)
│   └── demo.py                    # End-to-end demo script
├── evals/
│   └── retrieval_cases.example.jsonl   # Example evaluation cases (adapt to your KB)
├── tests/                         # pytest suite; Azure and the agent are faked (conftest.py blocks real clients)
├── .github/workflows/ci.yml       # Lint + tests + image build on every PR
├── .github/workflows/deploy.yml   # Manual azd deployment (OIDC)
├── .github/dependabot.yml
├── Dockerfile
├── docker-compose.yml
├── requirements.in                # Direct dependencies
├── requirements.txt               # Lock file generated from requirements.in
├── pyproject.toml
├── .env.example
└── README.md
```

---

## License

MIT
