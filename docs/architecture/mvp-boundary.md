# RegEngine AI: Core Regulatory-Compliance MVP vs. Frozen Subsystems Boundary

## 1. Executive Summary

This document formalizes the architectural boundary between **Core Regulatory-Compliance MVP** subsystems and **Frozen / Non-MVP Experimental Subsystems** in RegEngine AI.

The primary objective is to keep RegEngine's active operational and cognitive surface strictly focused on deterministic regulatory translation and verification, while preserving all experimental and exploratory code in place for future research without runtime or deployment drag.

---

## 2. The Core Regulatory-Compliance MVP Pipeline

The Core MVP is defined as the deterministic 8-stage processing pipeline:

```
[1. PDF Regulatory Document]
             │ (Upload via API or Ingestion Poller)
             ▼
[2. Layout-Aware Ingestion & Dual Hashing] (app/parsing, app/storage)
             │   - Computes source_document_sha256 over raw PDF bytes
             │   - Computes extracted_text_sha256 over normalized text
             │   - Extracts layout-aware clauses; chunks deterministically
             ▼
[3. Multi-Agent Extraction & Logic Audit] (app/agents)
             │   - Extraction Agent structures obligations, numerical thresholds, entities
             │   - Logic Auditor independently verifies extraction fidelity
             │   - Executes with bounded concurrency (EXTRACTION_CONCURRENCY)
             ▼
[4. Canonical Fact Binding & Taxonomy] (app/regulatory)
             │   - Resolves SEBI entity taxonomy (Stockbroker, Depository, Clearing Corp, AMC)
             │   - Binds canonical fact models (upfront margin, segregation, reporting deadlines)
             ▼
[5. Deterministic Policy Compilation & HITL Gate] (app/compiler)
             │   - Compiles audited clauses to OPA Rego policies & JSON-Logic ASTs
             │   - Emits HITLReview records for ambiguous, qualitative, or conflicting clauses
             │   - Gated in AWAITING_HITL state; active_rules remain 0 until human approval
             ▼
[6. Human-in-the-Loop Approval & Zero-Downtime Hot-Reload] (app/api/hitl_review_routes, app/execution)
             │   - Authorized Compliance Officer reviews findings with Step-Up MFA
             │   - Approving all pending reviews promotes rule to active (DEPLOYED)
             │   - Redis pub/sub broadcasts hot-reload to worker pods without container restart
             ▼
[7. High-Throughput OPA Live Execution Engine] (app/execution, app/api/execution_routes)
             │   - Synchronously evaluates broker transaction payloads against compiled Rego policies
             │   - Returns deterministic allow / deny / flagged outcomes with cited clause violations
             ▼
[8. Cryptographic Append-Only Audit Ledger] (app/ledger)
                 - PostgreSQL, append-only, SHA-256 hash-chained blocks, QLDB-journal-inspired design per ADR-0003
                 - Writes immutable SHA-256 hash-chained journal entry to PostgreSQL (compliance_audit_ledger)
                 - Binds transaction ID to exact source_document_sha256 and clause_hash
                 - Database immutability triggers prevent UPDATE/DELETE operations; no AWS QLDB dependency
                 - On-demand verification CLI validates chain continuity and detects tampering
```

---

## 3. Subsystem Classification Matrix

| Subsystem Name | Directory Path | Role & Classification | Imported by Core MVP? | Startup / Runtime Decoupling |
|---|---|---|---|---|
| **Document Ingestion & Storage** | `app/parsing/`, `app/storage/` | **Core MVP** | **Yes** | Uses local filesystem (`STORAGE_BACKEND=local`) or S3. |
| **Extraction & Logic Audit** | `app/agents/` | **Core MVP** | **Yes** | Active production path uses fixed two-agent sequential CrewAI execution (`app.agents.crew`) with Qwen2.5-72B-Instruct primary via Hugging Face Inference or self-hosted endpoint, alongside deterministic offline execution (`LLM_PROVIDER=offline`). The dynamic LangGraph orchestration layer (`app/agents/graph/`) and its Qwen2.5-7B-Instruct confidence fallback are an implemented, in-progress capability gated behind `settings.agent_graph_orchestration_enabled=False` (dormant by default). |
| **Taxonomy & Canonical Facts** | `app/regulatory/` (SEBI) | **Core MVP** | **Yes** | In-process canonical schemas. |
| **Compiler & HITL Gate** | `app/compiler/` | **Core MVP** | **Yes** | In-process Rego and AST compiler. |
| **HITL Review Management** | `app/api/hitl_review_routes.py` | **Core MVP** | **Yes** | PostgreSQL review records. |
| **Policy Registry & Hot-Reload** | `app/execution/policy_*` | **Core MVP** | **Yes** | In-process L1 cache + Redis L2 pub/sub. |
| **OPA Transaction Evaluation** | `app/execution/evaluator.py` | **Core MVP** | **Yes** | Co-located OPA server over HTTP. |
| **Cryptographic Audit Ledger** | `app/ledger/` | **Core MVP** | **Yes** | PostgreSQL, append-only, SHA-256 hash-chained blocks, QLDB-journal-inspired design per ADR-0003. |
| **Auth & Security** | `app/security/` | **Core MVP** | **Yes** | JWT, RBAC, step-up MFA, tenant crypto. |
| **Zero-Knowledge Proofs (ZKP)** | `app/zkp/` | **Frozen / Non-MVP** | **No** | Gated behind `settings.zkp_enabled=False`. |
| **FIX Protocol Gateway** | `app/fix_gateway/` | **Frozen / Non-MVP** | **No** | Gated behind `settings.fix_gateway_enabled=False`. |
| **Trade Conflict Negotiation** | `app/negotiation/` | **Frozen / Non-MVP** | **No** | Standalone multi-agent consensus module. |
| **Self-Healing Policy Loop** | `app/healing/` | **Frozen / Non-MVP** | **No** | Gated behind `settings.policy_self_healing_enabled=False`. |
| **Automated Grievance Escalation**| `app/grievance_escalation/` | **Frozen / Non-MVP** | **No** | Gated behind `settings.grievance_escalation_enabled=False`. Removed from default Celery beat schedule. |
| **Shadow Canary Traffic** | `app/canary/` | **Frozen / Non-MVP** | **No** | Gated behind `settings.canary_enabled=False`. Removed from default Celery beat schedule. |
| **Regulatory Filing Adapter** | `app/regulatory_filing/` | **Frozen / Non-MVP** | **No** | Gated behind `settings.regulatory_filing_enabled=False`. Removed from default Celery beat schedule. |
| **Multi-Regulator Extensions** | `app/regulatory/` (RBI/IRDAI/PFRDA)| **Frozen / Non-MVP** | **No** | Core MVP defaults to `Regulator.SEBI`. External sources (`app/ingestion/regulator_sources.py`) are optional. |
| **Multilingual OCR & Translation**| `app/localization/`, `app/translation_parity/` | **Frozen / Non-MVP** | **No** | Gated behind `settings.localization_enabled=False`. |
| **Historical Backtesting** | `app/backtest/` | **Frozen / Non-MVP** | **No** | Standalone batch replay engine. |
| **Intermediary Sandbox** | `app/sandbox/` | **Frozen / Non-MVP** | **No** | Standalone RLS tenant simulation router. |
| **Legal Knowledge Graph** | `app/graph/` | **Frozen / Non-MVP** | **No** | Gated behind `settings.neo4j_sync_enabled=False`. Requires Neo4j. |
| **Incident Breach Broadcaster** | `app/incident/` | **Frozen / Non-MVP** | **No** | Gated behind `settings.incident_broadcast_enabled=False` in `app/main.py` lifespan. |
| **Regulatory Version Diffing** | `app/diffing/` | **Frozen / Non-MVP** | **No** | Standalone master circular diffing router. |
| **Operational Analytics** | `app/analytics/`, `app/llm_ops/` | **Frozen / Non-MVP** | **No** | Telemetry and cost tracking routers. |
| **LLM Fine-Tuning Scaffolding** | `llm_finetune/` | **Frozen / Non-MVP** | **No** | A cost-tiered fine-tuning pipeline (QLoRA) is implemented for a self-hosted low-cost model tier; production fine-tuning on a real annotated SEBI corpus is a roadmap item, not yet complete. Synthetic fixtures provide smoke-test scaffolding only. |

---

## 4. Architectural Rules for Maintaining the Boundary

1. **No Application Imports from Frozen Modules**:
   Modules in the Core MVP pipeline (`app/parsing`, `app/agents`, `app/compiler`, `app/execution`, `app/ledger`, `app/storage`, `app/services`) must **never** import from frozen subsystems (`app/zkp`, `app/fix_gateway`, `app/negotiation`, `app/healing`, `app/canary`, `app/regulatory_filing`, `app/localization`, etc.).
2. **Core Startup Independence**:
   Starting `app.main:app` or running Celery workers must **never** fail or raise errors due to the absence of external non-MVP services (such as Neo4j, SFTP servers, SCORES APIs, Twilio/PagerDuty, or Circom verification keys).
3. **Preservation of Non-MVP Code**:
   Non-MVP subsystems must remain in the repository with complete git history and working unit tests. They must not be deleted or aggressively refactored unless prioritized by product requirements.
4. **Conditional Task Scheduling**:
   Periodic Celery beat tasks for non-MVP features must only be added to `celery_app.conf.beat_schedule` when their respective feature flags are explicitly set to `True` in environment configuration.
