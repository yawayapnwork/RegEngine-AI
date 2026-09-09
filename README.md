# RegEngine AI

**RegEngine AI turns SEBI and regulatory Master Circular PDFs into executable, auditable compliance controls** — from layout-aware ingestion and bounded-concurrency AI rule extraction/audit, into compiled OPA Rego policies, live transaction enforcement, and a tamper-evident PostgreSQL audit ledger. Every stage that cannot be resolved deterministically (such as qualitative directives or conflicting numerical thresholds) routes to a human-in-the-loop (HITL) review queue instead of guessing.

```mermaid
flowchart LR
    A["<b>Ingestion</b><br/>Circular PDF upload<br/>Layout-aware parse<br/>(Unstructured/OCR) +<br/>Dual SHA-256 hashes"]
    B["<b>Extraction & Audit</b><br/>CrewAI dual-agent<br/>extract +<br/>Logic Auditor<br/>(Bounded concurrency)"]
    C["<b>Compilation & HITL</b><br/>Rego + JSON-Logic<br/>compilation +<br/>blocking HITL gate"]
    D["<b>Execution</b><br/>FastAPI + embedded<br/>OPA engine, Celery<br/>batch/CDC, Redis<br/>hot-reload"]
    E["<b>Audit</b><br/>PostgreSQL<br/>append-only<br/>hash-chained<br/>ledger (SHA-256)"]
    F["React Dashboard<br/>(Pipeline, Split-View, Playground,<br/>HITL Review, Audit Vault)"]

    A --> B --> C --> D --> E
    F -.-> C
    F -.-> D
```

---

## Contents

- [Architecture & Processing Flow](#architecture--processing-flow)
- [Core MVP Architecture vs. Frozen Subsystems](#core-mvp-architecture-vs-frozen-subsystems)
- [Repository Layout](#repository-layout)
- [Prerequisites](#prerequisites)
- [Installation & Dependency Structure](#installation--dependency-structure)
- [Quickstart](#quickstart)
- [Environment & Deployment Modes](#environment--deployment-modes)
- [Configuration Reference](#configuration-reference)
- [Storage Architecture & Local Fallback](#storage-architecture--local-fallback)
- [LLM Provider & Model Architecture](#llm-provider--model-architecture)
- [Canonical End-to-End Workflows](#canonical-end-to-end-workflows)
- [API Surface](#api-surface)
- [Testing Guide](#testing-guide)
- [Operational & Hardening Considerations](#operational--hardening-considerations)
- [Model Fine-Tuning & Roadmap](#model-fine-tuning--roadmap)

---

## Architecture & Processing Flow

| Stage | Key Modules | What it does |
|---|---|---|
| **1. Ingestion & Provenance** | `app.parsing`, `app.storage`, `app.vectorstore` | Ingests circular PDFs via local filesystem or S3. Computes two distinct SHA-256 digests: `source_document_sha256` (over raw uploaded PDF bytes) and `extracted_text_sha256` (over normalized extracted text). Parses layout-aware clauses (`unstructured` with OCR/Tika fallbacks), chunks clauses deterministically, and indexes clause embeddings into Qdrant. State: `INGESTED`. |
| **2. Extraction & Audit** | `app.agents`, `app.services.orchestrator` | Runs dual-agent compliance analysis with bounded concurrency (preventing worker memory and provider rate-limit exhaustion). Backed by an open-weight model strategy: **Qwen2.5-72B-Instruct** primary with **Qwen2.5-7B-Instruct** fallback on low confidence (<0.85) via Hugging Face Inference or self-hosted endpoint, alongside deterministic offline execution (`LLM_PROVIDER=offline`). An **Extraction Agent** structures clauses into obligations, numerical thresholds, and entity targets; an independent **Logic Auditor Agent** verifies extraction fidelity before downstream compilation. State: `EXTRACTING` &rarr; `EXTRACTED`. *(Note: A cost-tiered fine-tuning pipeline (QLoRA) is implemented for a self-hosted low-cost model tier; production fine-tuning on a real annotated SEBI corpus is a roadmap item, not yet complete).* |
| **3. Compilation & HITL Gate** | `app.compiler`, `app.api.hitl_review_routes` | Compiles audited deterministic clauses into **OPA Rego** policies and structurally validated **JSON-Logic** ASTs. Any ambiguous, qualitative, low-confidence, or conflicting clauses generate `HITLReview` records. A rule can **never** become active while any blocking HITL review remains unresolved. State: `COMPILING` &rarr; `AWAITING_HITL`. |
| **4. Approval & Hot-Reload** | `app.execution.publisher`, `app.execution.policy_hot_reload` | Authorized compliance officers review and approve flagged items (protected by step-up MFA). Approving all blocking reviews transitions the rule to active and publishes it to OPA. A Redis pub/sub subscriber notifies worker pods to hot-reload OPA and invalidate local L1 caches without requiring container restarts. State: `APPROVED` &rarr; `DEPLOYED`. |
| **5. Execution Engine** | `app.execution`, `app.api.execution_routes` | Evaluates live broker transactions against deployed OPA policies, returning synchronous `allow`, `deny`, or `flagged` decisions. Asynchronous batch files and database CDC events are processed via dedicated Celery/Redis worker queues. |
| **6. Cryptographic Audit Ledger** | `app.ledger` | Realized as a PostgreSQL-native, append-only ledger with SHA-256 hash-chained blocks following a QLDB-journal-inspired design per [ADR-0003](docs/adr/0003-sha256-hash-chain-audit-log.md) (not an external AWS QLDB dependency). Every compliance evaluation is written to an immutable ledger table protected by database triggers. Each record binds the transaction to the exact circular and clause hashes that governed the decision. An on-demand verification CLI verifies chain continuity and pinpoints any tampered block. |
| **7. Dashboard UI** | `frontend/` | React (Vite + Tailwind CSS) client driven directly by the backend REST API: circular pipeline tracking, side-by-side legal text/Rego split view, zero-latency in-memory Policy Playground (OPA Wasm), HITL review portal, and live transaction audit vault. |

---

## Core MVP Architecture vs. Frozen Subsystems

RegEngine's active operational scope is strictly focused on the core regulatory-compliance MVP pipeline:

$$\text{PDF regulatory document} \longrightarrow \text{extraction} \longrightarrow \text{clause interpretation} \longrightarrow \text{canonical facts} \longrightarrow \text{deterministic policy compilation} \longrightarrow \text{HITL review} \longrightarrow \text{policy activation} \longrightarrow \text{OPA evaluation} \longrightarrow \text{cryptographic evidence/audit}$$

### Subsystem Classification Matrix

To keep the production and CI footprint lean without losing valuable prospective code, RegEngine explicitly segregates core components from frozen/experimental extensions. Frozen directories are retained in the codebase but removed from the default startup, runtime, and background execution paths.

For full architectural details, rules, and runtime guarantees, see [`docs/architecture/mvp-boundary.md`](docs/architecture/mvp-boundary.md).

| Directory / Feature | Status | In Core Path? | Description & Isolation Mode |
|---|---|:---:|---|
| `app/parsing` | **Core MVP** | Yes | PDF extraction, layout-aware chunking, dual SHA-256 digests. |
| `app/agents` | **Core MVP** | Yes | CrewAI dual-agent extraction and audit under bounded concurrency. |
| `app/compiler` | **Core MVP** | Yes | Deterministic Rego & JSON-Logic compilation, HITL ambiguity flagging. |
| `app/execution` | **Core MVP** | Yes | Policy evaluator, OPA client/publisher, hot-reload, Celery batch worker. |
| `app/services` | **Core MVP** | Yes | Orchestrator, HITL lifecycle service, circular processing pipeline. |
| `app/ledger` | **Core MVP** | Yes | PostgreSQL, append-only, SHA-256 hash-chained blocks, QLDB-journal-inspired design per ADR-0003. |
| `app/db` | **Core MVP** | Yes | Relational models (`Circular`, `Clause`, `CompiledRule`, `HITLReview`, `Ledger`). |
| `app/api` | **Core MVP** | Yes | REST routes (`/v1/circulars`, `/v1/execution`, `/v1/hitl-reviews`, `/v1/auth`). |
| `app/storage` | **Core MVP** | Yes | Local filesystem and S3 storage abstraction. |
| `app/security` | **Core MVP** | Yes | JWT auth, RBAC, step-up MFA for compliance approval actions. |
| `frontend/` | **Core MVP** | Yes | React dashboard wired to backend REST APIs (no mock dependencies). |
| `app/zkp` | **Frozen / Experimental** | No | Zero-knowledge proof compliance proofs (preserved, not invoked). |
| `app/fix_gateway` | **Frozen / Experimental** | No | Financial Information eXchange (FIX) protocol bridge. |
| `app/negotiation` | **Frozen / Experimental** | No | Agent-to-agent regulatory clarification negotiation. |
| `app/healing` | **Frozen / Experimental** | No | Autonomous policy self-healing from execution anomalies. |
| `app/grievance_escalation` | **Frozen / Experimental** | No | SEBI SCORES / investor grievance automation (beat tasks gated off). |
| `app/canary` | **Frozen / Experimental** | No | Canary policy deployments and rollback windows (beat tasks gated off). |
| `app/regulatory_filing` | **Frozen / Experimental** | No | Automated regulatory filing generation (beat tasks gated off). |
| `app/localization` | **Frozen / Experimental** | No | Multilingual OCR and vernacular circular processing. |
| `app/translation_parity` | **Frozen / Experimental** | No | Cross-lingual legal translation verification and parity scoring. |
| `app/backtest` | **Frozen / Experimental** | No | Historical market backtesting engine for draft policies. |
| `app/graph` | **Frozen / Experimental** | No | Circular-to-clause dependency knowledge graph engine. |
| `app/diffing` | **Frozen / Experimental** | No | Regulatory supersession diffing and amendatory clause tracking. |
| `app/incident` | **Frozen / Experimental** | No | Real-time breach notification WebSockets (gated behind `incident_broadcast_enabled=False`). |
| `llm_finetune/` | **Scaffolding / Roadmap** | No | A cost-tiered fine-tuning pipeline (QLoRA) is implemented for a self-hosted low-cost model tier; production fine-tuning on a real annotated SEBI corpus is a roadmap item, not yet complete. Synthetic fixtures provide smoke-test scaffolding only. |
| Multi-Regulator (RBI/IRDAI/PFRDA) | **Frozen / Experimental** | No | `Regulator.SEBI` is the sole active core MVP regulator; others marked frozen. |

### Architectural Boundary Guarantees

1. **Zero-Dependency Core Startup**: The core application (`uvicorn app.main:app`) does not initialize or require services, brokers, or periodic background tasks associated with non-MVP features.
2. **Feature Flags**: Experimental Celery beat tasks and WebSocket subscribers default to `False` (`settings.canary_enabled`, `settings.regulatory_filing_enabled`, `settings.grievance_escalation_enabled`, `settings.incident_broadcast_enabled`).
3. **No Blind Deletion**: All frozen subsystems remain present in the tree with standard `[FROZEN / NON-MVP EXPERIMENTAL SUBSYSTEM]` banners for future development.

---

## Repository Layout

```
app/
  # Core Regulatory-Compliance MVP Subsystems
  parsing/          [Core MVP] PDF extraction, chunking, cryptographic hashing (raw vs text)
  vectorstore/      [Core MVP] Embeddings + Qdrant indexing
  agents/           [Core MVP] CrewAI dual-agent extraction / logic audit pipelines, schemas
  compiler/         [Core MVP] Rego + JSON-Logic compilers, naming conventions, HITL flagging
  execution/        [Core MVP] Evaluator, OPA client, policy publisher, Celery tasks, HITL queue
  services/         [Core MVP] Orchestrator, HITL review lifecycle service, pipeline coordination
  ledger/           [Core MVP] PostgreSQL append-only SHA-256 hash-chained audit ledger (QLDB-journal-inspired design per ADR-0003), verifier CLI
  db/               [Core MVP] SQLAlchemy ORM models (circulars, clauses, rules, reviews, ledger)
  storage/          [Core MVP] Storage abstraction: LocalFilesystemStorage and S3Storage
  security/         [Core MVP] OAuth2/JWT auth, RBAC, step-up MFA, tenant crypto, secrets backends
  api/              [Core MVP] Core FastAPI routers (circulars, execution, hitl-reviews, auth)
  main.py           [Core MVP] FastAPI application assembly and lifecycle handlers
  config.py         [Core MVP] Centralized environment-driven settings (Pydantic Settings)

  # Frozen / Non-MVP Experimental Subsystems (Preserved, decoupled from core runtime)
  zkp/                  [Frozen] Zero-knowledge proof compliance proofs
  fix_gateway/          [Frozen] FIX protocol order processing gateway
  negotiation/          [Frozen] Autonomous agent-to-agent regulatory negotiation
  healing/              [Frozen] Autonomous policy self-healing from incident telemetry
  grievance_escalation/ [Frozen] SEBI SCORES / investor grievance escalation workflows
  canary/               [Frozen] Canary policy routing and rollback automation
  regulatory_filing/    [Frozen] Automated regulatory compliance filing generation
  localization/         [Frozen] Multilingual OCR and vernacular document extraction
  translation_parity/   [Frozen] Cross-lingual legal translation parity validation
  backtest/             [Frozen] Historical trade compliance backtesting
  graph/                [Frozen] Cross-circular dependency knowledge graph
  diffing/              [Frozen] Regulatory supersession and amendatory diffing
  incident/             [Frozen] WebSocket breach event streaming
  llm_finetune/         [Scaffolding / Roadmap] QLoRA fine-tuning pipeline scaffolding and synthetic fixture generator
frontend/
  src/constants/    Canonical constants (pipeline stage definitions, labels)
  src/components/   UI views: pipeline, splitview, playground, hitl, vault, layout
  src/api/          REST clients for backend API endpoints
sql/
  ledger_schema.sql PostgreSQL DDL for audit ledger (immutability triggers, indexes)
migrations/         Alembic schema migrations
tests/              Pytest test suite (compiler, agents, parsing, ledger, API)
pyproject.toml      Canonical packaging metadata and dependency definitions
requirements.txt    Mirrored runtime dependencies for Docker layer caching
```

---

## Prerequisites

| Dependency | Supported Versions | Purpose | Notes |
|---|---|---|---|
| **Python** | 3.11, 3.12 (`>=3.11`) | Backend application and CLI tools | Python 3.11 is used in CI; Python 3.12-slim is used in production Dockerfiles. |
| **Node.js** | 18+ | Frontend dashboard | Bundled with Vite and Tailwind CSS. |
| **PostgreSQL** | 14+ (16 recommended) | Main relational schema & Audit Ledger | Hosts the append-only SHA-256 hash-chained ledger per ADR-0003. Requires `sql/ledger_schema.sql` applied before migrations. |
| **Redis** | 6+ (7 recommended) | Celery broker/backend, policy registry, hot-reload pub/sub | |
| **Open Policy Agent** | 0.68+ | Embedded policy evaluation | Run as local/sidecar server: `opa run --server`. |
| **Qdrant** | 1.10+ | Vector database for clause retrieval | Optional for offline tests; required for vector search. |
| **Object Storage** | Local directory or S3 | Storing uploaded PDF documents | Defaults to `STORAGE_BACKEND=local` (`data/uploads`). MinIO / S3 is optional. |

---

## Installation & Dependency Structure

`pyproject.toml` is the **canonical dependency definition** for RegEngine AI. `requirements.txt` mirrors the core runtime dependencies to ensure Docker build layer caching and CI vulnerability scanner compatibility.

### 1. Runtime Installation

To install only the core runtime dependencies:

```bash
# Using canonical packaging metadata:
pip install .

# Or using mirrored requirements:
pip install -r requirements.txt
```

### 2. Development & Test Installation

Test and development dependencies are separated into optional extras in `pyproject.toml`:

```bash
# Install editable with test runners (pytest, pytest-asyncio, aiosqlite, pytest-cov):
pip install -e ".[test]"

# Install editable with full dev tooling (test dependencies + ruff, mypy, bandit, type stubs):
pip install -e ".[dev]"
```

### 3. Optional Provider Backends

```bash
pip install -e ".[sso]"     # Enterprise SAML 2.0 (python3-saml)
pip install -e ".[aws]"     # AWS Secrets Manager & S3 (boto3)
pip install -e ".[vault]"   # HashiCorp Vault KV v2 (hvac)
```

---

## Quickstart

### Step 1: Clone and Configure Environment

```bash
cp .env.example .env
```

Review `.env` settings. For local testing without external API tokens, set:
- `ENVIRONMENT=development`
- `LLM_PROVIDER=offline`
- `STORAGE_BACKEND=local`

### Step 2: Start Infrastructure Services

Using Docker Compose:

```bash
# Start PostgreSQL, Redis, OPA, and Qdrant:
docker compose up -d postgres redis opa qdrant
```

### Step 3: Initialize Database Schemas

Apply the immutable audit ledger schema and Alembic migrations:

```bash
# 1. Apply append-only ledger DDL and immutability triggers:
psql "postgresql://regengine:changeme@localhost:5432/regengine" -f sql/ledger_schema.sql

# 2. Run Alembic schema migrations:
alembic upgrade head
```

### Step 4: Start Backend API & Worker

```bash
# Terminal 1: FastAPI Web Service
uvicorn app.main:app --reload --port 8000

# Terminal 2: Celery Background Worker
celery -A app.execution.celery_app worker \
  -Q regengine_batch,regengine_cdc,regengine_webhooks,regengine_ingestion,regengine_agents,regengine_compiler,regengine_vectorstore \
  -l info
```

### Step 5: Start Frontend Dashboard

```bash
cd frontend
npm install
npm run dev
# Dashboard is live at http://localhost:5173
```

---

## Environment & Deployment Modes

RegEngine AI enforces explicit environment boundaries to prevent insecure configurations from operating in staging or production.

```
+-----------------------------------------------------------------------------------+
| Mode          | ENVIRONMENT    | DEMO_MODE | Behavior                             |
|---------------+----------------+-----------+--------------------------------------|
| Development   | development    | false     | Normal local dev. Real auth checks;  |
|               |                |           | requires real step-up MFA claims.    |
|---------------+----------------+-----------+--------------------------------------|
| Demo          | development    | true      | Isolated demo flow. Issues test MFA  |
|               |                |           | claims (amr=["pwd","mfa"]); logs     |
|               |                |           | prominent startup warnings.          |
|---------------+----------------+-----------+--------------------------------------|
| Staging       | staging        | false     | Pre-production validation. Rejects   |
|               |                |           | DEMO_MODE=true; warns on weak keys.  |
|---------------+----------------+-----------+--------------------------------------|
| Production    | production     | false     | Strict enforcement. FATAL crash if   |
|               |                |           | DEMO_MODE=true or default secrets    |
|               |                |           | are detected at startup.             |
+-----------------------------------------------------------------------------------+
```

> [!CAUTION]
> `DEMO_MODE=true` is **strictly prohibited** in `staging` and `production`. The application checks this during startup and refuses to boot if misconfigured.

---

## Configuration Reference

Tunables are configured via environment variables (loaded from `.env` in local development):

| Variable | Default | Purpose |
|---|---|---|
| `ENVIRONMENT` | `development` | Environment tier: `development`, `staging`, or `production`. |
| `DEMO_MODE` | `false` | Enables test MFA claims in development mode only. Prohibited in staging/production. |
| `STORAGE_BACKEND` | `local` | `local` (filesystem in `data/uploads`) or `s3` (S3/MinIO). |
| `STORAGE_LOCAL_DIR` | `data/uploads` | Path for local filesystem PDF storage when `STORAGE_BACKEND=local`. |
| `LLM_PROVIDER` | `offline` | Extraction LLM provider: `offline` (deterministic local), `huggingface` (production open-weight), `openai`, `anthropic`. |
| `HUGGINGFACEHUB_API_TOKEN` | — | API token for Hugging Face Inference (or set `HF_TOKEN`). |
| `HF_MODEL_ID` | `Qwen/Qwen2.5-72B-Instruct` | Primary open-weight model for extraction and logic audit. |
| `AGENT_FALLBACK_MODEL` | `huggingface/Qwen/Qwen2.5-7B-Instruct` | Fallback model invoked when extraction confidence < 0.85 in dynamic agent graph. |
| `DATABASE_URL` | `postgresql+asyncpg://...` | Connection URI for the main relational schema. |
| `LEDGER_DATABASE_URL` | `postgresql+asyncpg://...` | Connection URI for the append-only audit ledger. |
| `REDIS_URL` | `redis://localhost:6379/0` | Celery broker/backend, policy registry, and hot-reload pub/sub. |
| `OPA_SERVER_URL` | `http://localhost:8181` | Address of the co-located Open Policy Agent server. |
| `QDRANT_URL` | `http://localhost:6333` | Vector database endpoint for clause embeddings. |
| `JWT_SECRET_KEY` | — | Secret key for signing internal JWT tokens. Must be changed in production. |
| `SECRETS_BACKEND` | `env` | Key resolution backend: `env`, `aws` (AWS Secrets Manager), or `vault` (HashiCorp Vault). |
| `EXTRACTION_CONCURRENCY` | `4` | Maximum bounded concurrency for parallel clause extraction/auditing. |

---

## Storage Architecture & Local Fallback

RegEngine AI provides a storage abstraction (`app/storage/object_store.py`) that decouples file ingestion from third-party cloud dependencies:

1. **Local Filesystem Storage (`STORAGE_BACKEND=local`)**:
   - **Default mode** for POCs, unit tests, and development.
   - Uploaded circular PDFs are stored under `STORAGE_LOCAL_DIR` (`data/uploads`).
   - Generates safe UUID-based file paths to prevent directory traversal attacks.
   - Does not require MinIO, AWS S3, or network storage.
2. **S3-Compatible Object Storage (`STORAGE_BACKEND=s3`)**:
   - For multi-node deployments, Kubernetes clusters, or cloud staging/production.
   - Supports AWS S3, MinIO, Cloudflare R2, and Backblaze B2.
   - Requires `OBJECT_STORAGE_ENDPOINT_URL`, `OBJECT_STORAGE_BUCKET`, `OBJECT_STORAGE_ACCESS_KEY_ID`, and `OBJECT_STORAGE_SECRET_ACCESS_KEY`.

---

## LLM Provider & Model Architecture

For the complete architectural specification, see [`docs/architecture/llm-provider-architecture.md`](docs/architecture/llm-provider-architecture.md).

### 1. Model & Provider Matrix

| Provider Identifier | Supported Status | Default / Configured Model | Pipeline Role | Executed in Production? |
|---|---|---|---|:---:|
| `huggingface` | **Active Production** | `Qwen/Qwen2.5-72B-Instruct` | **Primary Extraction & Logic Audit** | **Yes** (when `LLM_PROVIDER=huggingface`) |
| `huggingface` | **Active Fallback** | `huggingface/Qwen/Qwen2.5-7B-Instruct` | **Low-Confidence Fallback** (confidence < 0.85) | **Yes** (conditional on confidence gate) |
| `offline` | **Active Default** | Deterministic Regex Engine | **Testing, CI & Air-Gapped Demos** | **Yes** (default out-of-the-box mode) |
| `openai` | **Supported Abstraction** | `gpt-4o` | Alternative provider adapter | **No** (dormant in standard pipeline) |
| `anthropic` | **Supported Abstraction** | `claude-3-5-sonnet-20241022` | Alternative provider adapter | **No** (dormant in standard pipeline) |
| `sebi-compliance-llm` | **Roadmap Scaffolding** | `sebi-compliance-llm` | Low-cost tier scaffolding (`llm_finetune/`) | **No** (production training is on roadmap) |

### 2. Single-Provider Open-Weight Architecture Rationale
- **Data Sovereignty & Deployment Flexibility**: Utilizing open-weight models allows deployment either via Hugging Face Inference API (cloud staging) or containerized on-premises VPC hosting (vLLM / TGI) to comply with data localization mandates without code changes.
- **Consistent Formatting & Tokenizer Geometry**: Keeping primary (72B) and fallback (7B) in the same model family minimizes schema drift, markdown wrapping anomalies, and prompt template discrepancies.
- **Dual-Model Cascade**: Ambiguous or low-confidence extractions (<0.85) escalate to the 7B checkpoint in `app.agents.graph` to provide diverse extraction hypotheses prior to Logic Auditor verification.
- **No Proprietary Cloud Model Execution**: No live calls to Claude, GPT-4, or Llama models are executed in the production pipeline; provider abstractions exist strictly for architectural extensibility.

---

## Canonical End-to-End Workflows

### 1. The CLI Pipeline Runner (`regengine-cli.py`)

The CLI runner exercises the end-to-end pipeline using the same service code paths as the API and Celery workers:

```bash
# Run full E2E pipeline offline (no external LLM token required):
python regengine-cli.py --offline-agents

# Ingest a specific circular PDF from a file:
python regengine-cli.py --pdf ./samples/sebi_circular.pdf --offline-agents

# Dry-run parsing and compilation only (no running OPA or PostgreSQL needed):
python regengine-cli.py --dry-run
```

### 2. The REST API Ingestion & Evaluation Workflow

```bash
# 1. Ingest PDF and execute automated extraction & rule compilation:
curl -X POST http://localhost:8000/v1/circulars/process-e2e \
  -H "Authorization: Bearer <ADMIN_OR_OFFICER_TOKEN>" \
  -F "file=@circular.pdf"

# 2. Check circular status and compiled rules:
curl http://localhost:8000/v1/circulars/1/status \
  -H "Authorization: Bearer <TOKEN>"

# 3. List pending Human-in-the-Loop review cases:
curl http://localhost:8000/v1/hitl-reviews \
  -H "Authorization: Bearer <OFFICER_TOKEN>"

# 4. Approve a flagged rule (activates rule and hot-reloads OPA):
curl -X POST http://localhost:8000/v1/hitl-reviews/review-123/approve \
  -H "Authorization: Bearer <OFFICER_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"notes": "Verified margin calculation against master circular section 3.2"}'

# 5. Evaluate a live transaction against deployed policies:
curl -X POST http://localhost:8000/v1/execution/transactions/evaluate \
  -H "Authorization: Bearer <BROKER_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{
    "transaction_id": "TXN-2026-001",
    "broker_id": "BRK-001",
    "entity_type": "Stockbroker",
    "facts": { "upfront_margin_pct": 25.0 }
  }'

# 6. Verify audit ledger cryptographic integrity:
python -m app.ledger.verify_cli --start 2026-01-01 --end 2026-12-31
```

---

## API Surface

### Circular Ingestion & Pipeline (`/v1/circulars`)
- `POST /v1/circulars/process-e2e`: Accepts a PDF, parses layout, extracts rules with bounded concurrency, compiles Rego, flags HITL reviews.
- `GET /v1/circulars`: Lists ingested circulars with aggregated clause, active rule, and review metrics.
- `GET /v1/circulars/{circular_id}`: Retrieves details, clauses, and rule compilation state for a circular.
- `GET /v1/circulars/{circular_id}/status`: Current lifecycle state (`INGESTED`, `EXTRACTING`, `COMPILING`, `AWAITING_HITL`, `DEPLOYED`).

### Human-in-the-Loop Reviews (`/v1/hitl-reviews`)
- `GET /v1/hitl-reviews`: Lists pending or resolved review cases.
- `GET /v1/hitl-reviews/{review_id}`: Inspects rule diff, extracted threshold, and auditor findings.
- `POST /v1/hitl-reviews/{review_id}/approve`: Approves flagged policy (requires Compliance Officer role and step-up MFA).
- `POST /v1/hitl-reviews/{review_id}/reject`: Rejects policy from deployment.

### Policy Execution & Ledger (`/v1/execution`)
- `POST /v1/execution/transactions/evaluate`: Evaluates transactions against OPA policies and writes an append-only audit ledger entry.
- `GET /v1/execution/ledger/entries`: Fetches recent audit ledger entries.
- `POST /v1/execution/ledger/verify`: Verifies hash chain integrity across stored blocks.

> [!NOTE]
> The cryptographic audit vault is implemented entirely natively in PostgreSQL using append-only, SHA-256 hash-chained blocks (a QLDB-journal-inspired design per [ADR-0003](docs/adr/0003-sha256-hash-chain-audit-log.md)), requiring no AWS QLDB or external managed ledger service.

### Authentication (`/v1/auth`)
- `POST /v1/auth/login`: Authenticates user credentials and issues JWT token.
- `POST /v1/auth/token`: OAuth2 client credentials grant for machine clients.
- `GET /v1/auth/me`: Returns current caller principal, roles, and authentication claims.

---

## Testing Guide

The test suite is structured into unit, integration, and end-to-end tests:

```bash
# Run fast isolated unit tests:
pytest -m "not slow and not integration"

# Run integration tests (requires local PostgreSQL and Redis):
pytest -m integration

# Run full test suite with coverage:
pytest --cov=app --cov-report=term-missing

# Run a specific test module:
pytest tests/test_compiler.py -v
pytest tests/test_circular_resumability.py -v
```

- **Unit Tests**: Utilize SQLite (`aiosqlite`) in-process for speed; external network endpoints (Hugging Face Inference, S3) are mocked via standard test doubles.
- **Integration Tests**: Exercise PostgreSQL advisory locks, Alembic migrations, Redis caching, and real OPA policy evaluation.

---

## Operational & Hardening Considerations

> [!NOTE]
> RegEngine AI is currently an **architectural reference implementation and functional prototype**. Organizations preparing for production deployment should address the hardening measures below.

1. **Database Role Separation**:
   - In production, configure separate database credentials for `DATABASE_URL` (ordinary CRUD permissions) and `LEDGER_DATABASE_URL` (`INSERT` and `SELECT` only, with `UPDATE` and `DELETE` revoked).
2. **Secrets Management**:
   - Set `SECRETS_BACKEND=aws` or `SECRETS_BACKEND=vault` to resolve JWT signing keys, database passwords, and webhook secrets from a secure vault rather than environment files.
3. **Enterprise Identity & Step-Up MFA**:
   - Replace development authentication with enterprise SSO (`jwt_jwks_url` for OIDC or `python3-saml` for SAML 2.0).
   - Require real Time-based One-Time Password (TOTP) or FIDO2 WebAuthn hardware tokens for compliance approval actions.
4. **Offline Container Building**:
   - The multi-stage `Dockerfile` pre-warms ML layout detection weights during the builder stage. When deploying into restricted air-gapped environments, build container images in a network-accessible staging pipeline before promoting to private registries.
5. **CORS Configuration**:
   - In split frontend/backend deployments, explicitly configure `CORS_ALLOWED_ORIGINS` with the exact frontend domain (e.g. `["https://compliance.internal.bank"]`).

---

## Model Fine-Tuning & Roadmap

### Current Status: Scaffolding & Tiered Architecture
- **Active Extraction Path**: RegEngine AI's active extraction and logic auditing pipeline uses general-purpose frontier LLMs (such as Qwen2.5-72B-Instruct via Hugging Face Inference) and deterministic offline rule extractors (`LLM_PROVIDER=offline`).
- **Cost-Tiered Scaffolding (`llm_finetune/`)**: A cost-tiered fine-tuning pipeline (QLoRA) is implemented for a self-hosted low-cost model tier; production fine-tuning on a real annotated SEBI corpus is a roadmap item, not yet complete.
- **Synthetic Fixtures**: The fixtures in `llm_finetune/dataset/sample_artifacts.py` serve strictly as pipeline smoke tests and schema validation fixtures. They do not constitute regulatory training data.
- **No Unbenchmarked Accuracy Claims**: No accuracy improvements or domain-adapted performance gains are claimed prior to formal benchmarking against real regulatory ground-truth datasets.

### Roadmap Items
1. **Curated SEBI Regulatory Dataset**: Assemble and manually verify an annotated corpus of historical and current SEBI master circulars, amendments, and gazettes.
2. **Production QLoRA Training**: Execute supervised fine-tuning across domain-specific tokenizers and models once the curated corpus is complete.
3. **Empirical Benchmarking**: Benchmark extraction F1, numerical precision, and obligation classification accuracy against baseline models before promoting fine-tuned checkpoints to production.
