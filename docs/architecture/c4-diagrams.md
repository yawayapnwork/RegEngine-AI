# RegEngine AI — C4 Model

Four levels (Context, Container, Component, Code) per the [C4 model](https://c4model.com/),
plus two supplementary sequence diagrams for the two flows called out
explicitly: SEBI RSS ingest, and broker OMS/FIX order validation. All
diagrams are Mermaid.js — GitHub, GitLab, and most IDE Markdown
previews render these fences natively with no extra tooling.

Every element is tagged with its explicit capability status per the RegEngine PRD:
- **`[CURRENT]`**: Implemented, integrated into the live production pipeline, and verified end-to-end.
- **`[IN PROGRESS]`**: Implemented in code, but decoupled/feature-flagged from the default production pipeline.
- **`[ROADMAP]`**: Proposed or design-stage capability; not implemented in code.

## Level 1 — System Context

Who and what RegEngine AI talks to, and why.

```mermaid
C4Context
    title RegEngine AI — System Context

    Person(officer, "Compliance Officer [CURRENT]", "Reviews HITL-flagged rules/transactions, approves policy publication with step-up MFA")
    Person(inspector, "SEBI Inspector [CURRENT]", "Independently re-verifies audit-log integrity offline, with no access to RegEngine's servers or database")

    System_Ext(sebiSources, "SEBI Circular Sources [CURRENT]", "RSS feeds and HTML notice pages publishing new/amended circulars")
    System_Ext(brokerRest, "Broker OMS / RMS [CURRENT]", "Submits live transactions for real-time compliance evaluation via REST API")
    System_Ext(hfInference, "Hugging Face Inference / Self-Hosted [CURRENT]", "Qwen2.5-72B-Instruct (Active primary dual-agent extraction & audit; 7B fallback wired into in-progress LangGraph layer)")

    System_Ext(brokerOms, "Broker OMS via FIX [IN PROGRESS]", "Order Management System submitting orders via QuickFIX bridge (gated off by default)")
    System_Ext(scores, "SEBI SCORES Portal [IN PROGRESS]", "Regulator grievance-redress REST API (gated off by default)")

    System(regengine, "RegEngine AI [CURRENT]", "Extracts, compiles, executes, and audits SEBI compliance rules against live broker transactions")

    Rel(sebiSources, regengine, "Publishes new circulars", "RSS / HTTPS [CURRENT]")
    Rel(brokerRest, regengine, "Submits transactions for validation", "HTTPS / REST [CURRENT]")
    Rel(regengine, brokerRest, "Returns allow/deny decision + clause citations", "HTTPS / REST [CURRENT]")
    Rel(regengine, hfInference, "Extracts & audits compliance rules from clause text", "HTTPS / Hugging Face Inference API [CURRENT]")
    Rel(officer, regengine, "Reviews, approves, confirms", "HTTPS / Web UI [CURRENT]")
    Rel(inspector, regengine, "Downloads signed audit binder (offline afterward)", "HTTPS, one-time export [CURRENT]")

    Rel(brokerOms, regengine, "Submits orders via FIX bridge (feature-flagged)", "FIX 4.2/4.4 [IN PROGRESS]")
    Rel(regengine, scores, "Files grievance records (feature-flagged)", "HTTPS / REST [IN PROGRESS]")
```

## Level 2 — Containers

The deployable units inside RegEngine AI's system boundary.

```mermaid
C4Container
    title RegEngine AI — Containers

    Person(officer, "Compliance Officer [CURRENT]")
    System_Ext(sebiSources, "SEBI Circular Sources [CURRENT]")
    System_Ext(brokerRest, "Broker OMS / RMS [CURRENT]")
    System_Ext(hfInference, "Hugging Face Inference / Self-Hosted [CURRENT]")

    Container_Boundary(regengine, "RegEngine AI") {
        Container(frontend, "Compliance IDE [CURRENT]", "React + Tailwind", "Dashboards: HITL queue, policy split-view, rule playground, audit vault")
        Container(api, "FastAPI Application [CURRENT]", "Python 3.11 / FastAPI", "Synchronous REST surface: transaction evaluation, HITL, circular ingestion")
        Container(workers, "Celery Workers [CURRENT]", "Python / Celery", "Async pipeline: ingestion polling, bounded dual-agent extraction, Rego compilation")
        Container(opa, "OPA Server [CURRENT]", "Open Policy Agent", "Evaluates compiled Rego policy for synchronous REST transaction evaluations")

        ContainerDb(postgres, "PostgreSQL [CURRENT]", "App schema + Audit Ledger", "Circulars, clauses, compiled rules, HITL reviews, and PostgreSQL append-only SHA-256 hash-chained compliance_audit_ledger")
        ContainerDb(redis, "Redis [CURRENT]", "Cache / Queue / Pub-Sub", "Celery broker, policy registry (L2), hot-reload pub-sub")
        ContainerDb(qdrant, "Qdrant [CURRENT]", "Vector Store", "Clause embeddings for semantic retrieval")

        Container(fixGateway, "FIX Gateway [IN PROGRESS]", "Python (QuickFIX) + C++", "Intercepts broker NewOrderSingle messages (gated: fix_gateway_enabled=False)")
        Container(nativeKernel, "Native Policy Kernel [IN PROGRESS]", "C++17, header-only + C-ABI", "Allocation-free compiled-policy evaluator (embedded in FIX Gateway)")
        ContainerDb(neo4j, "Neo4j [IN PROGRESS]", "Knowledge Graph", "Circular/Clause knowledge graph (gated: neo4j_sync_enabled=False)")
    }

    Rel(sebiSources, workers, "Polled by ingestion tasks", "RSS / HTTPS [CURRENT]")
    Rel(workers, hfInference, "Extraction + Audit agent calls", "HTTPS [CURRENT]")
    Rel(workers, opa, "Publishes compiled Rego", "HTTPS Policy API [CURRENT]")
    Rel(workers, postgres, "Persists circulars/clauses/compiled rules", "SQL [CURRENT]")
    Rel(workers, qdrant, "Indexes clause embeddings", "gRPC / HTTP [CURRENT]")

    Rel(brokerRest, api, "Evaluates transactions via REST", "HTTPS [CURRENT]")
    Rel(api, opa, "Evaluates transactions", "HTTPS [CURRENT]")
    Rel(api, postgres, "Reads/writes app schema + appends to audit ledger", "SQL [CURRENT]")
    Rel(api, redis, "Policy cache, HITL queues, pub-sub", "Redis protocol [CURRENT]")
    Rel(api, frontend, "Serves REST", "HTTPS [CURRENT]")
    Rel(officer, frontend, "Uses", "HTTPS [CURRENT]")

    Rel(workers, neo4j, "Syncs compliance knowledge graph (dormant)", "Bolt [IN PROGRESS]")
    Rel(nativeKernel, redis, "Hot-reloaded from pub-sub (dormant)", "Redis [IN PROGRESS]")
```

## Level 3 — Components (inside the FastAPI Application container)

Zooming into the container most requests actually traverse.

```mermaid
C4Component
    title RegEngine AI — Components inside the FastAPI Application

    Container_Boundary(api, "FastAPI Application") {
        Component(evaluator, "Evaluator [CURRENT]", "app.execution.evaluator", "Reduces per-policy OPA outcomes to allow/deny/flagged (most-restrictive-wins)")
        Component(opaEngine, "OPAEngine [CURRENT]", "app.execution.opa_engine", "Async HTTP client to the co-located OPA server; publishes and evaluates policy")
        Component(policyCache, "PolicyCache / PolicyRegistry [CURRENT]", "app.execution.policy_cache/registry", "L1 in-process + L2 Redis view of which compiled policies apply to which entity_type")
        Component(hitlQueue, "HITLQueue [CURRENT]", "app.execution.hitl_queue", "Redis-backed queue of ambiguous live-transaction decisions awaiting human sign-off")
        Component(ledgerIntegration, "Ledger Integration [CURRENT]", "app.ledger.integration", "Maps one evaluation result onto hash-chained ledger rows in PostgreSQL")
        Component(killSwitch, "KillSwitchMiddleware [CURRENT]", "app.governance.middleware", "Halts evaluation platform-wide or per-tenant on operator command")

        Component(negotiation, "Negotiation Orchestrator [IN PROGRESS]", "app.negotiation", "Multi-agent consensus for cross-domain compliance conflicts (gated: negotiation_enabled=False)")
        Component(canary, "Canary Orchestrator [IN PROGRESS]", "app.canary", "Shadow-evaluates candidate policy against traffic (gated: canary_enabled=False)")
        Component(grievance, "Grievance Escalation [IN PROGRESS]", "app.grievance_escalation", "Detects systemic broker non-compliance; files SCORES (gated: grievance_escalation_enabled=False)")
        Component(incidentPublisher, "Incident Publisher [IN PROGRESS]", "app.incident.publisher", "Fans breach events out to WebSockets (gated: incident_broadcast_enabled=False)")
    }

    ContainerDb(opa, "OPA Server [CURRENT]")
    ContainerDb(postgres, "PostgreSQL [CURRENT]")
    ContainerDb(redis, "Redis [CURRENT]")
    System_Ext(brokerRest, "Broker OMS / RMS [CURRENT]")

    Rel(brokerRest, evaluator, "TransactionPayload", "via /v1/execution/transactions/evaluate [CURRENT]")
    Rel(evaluator, killSwitch, "Checked before evaluating [CURRENT]")
    Rel(evaluator, policyCache, "policies_for(entity_type) [CURRENT]")
    Rel(evaluator, opaEngine, "evaluate(package, input_doc) [CURRENT]")
    Rel(opaEngine, opa, "POST /v1/data/... [CURRENT]")
    Rel(evaluator, hitlQueue, "enqueue() on FLAGGED [CURRENT]")
    Rel(evaluator, ledgerIntegration, "log_evaluation(transaction, result) [CURRENT]")
    Rel(ledgerIntegration, postgres, "append_entry() — hash-chained insert [CURRENT]")

    Rel(ledgerIntegration, incidentPublisher, "raise_breach_event() on FAIL/HITL_REVIEW (dormant) [IN PROGRESS]")
    Rel(ledgerIntegration, grievance, "evaluate_and_trigger_grievance_escalation() (dormant) [IN PROGRESS]")
    Rel(negotiation, opaEngine, "Per-agent shadow evaluation (dormant) [IN PROGRESS]")
    Rel(canary, opa, "Publishes candidate under a namespaced package (dormant) [IN PROGRESS]")
```

## Level 4 — Code (the audit-ledger hash-chain module)

The most safety-critical single module in the platform: PostgreSQL, append-only, SHA-256 hash-chained blocks, QLDB-journal-inspired design per [ADR-0003](../adr/0003-sha256-hash-chain-audit-log.md) (with no active AWS QLDB dependency), at class/function granularity.

```mermaid
classDiagram
    class ComplianceEvaluationEvent {
        +str broker_id
        +str transaction_id
        +datetime evaluated_at
        +str circular_id
        +str clause_hash
        +str section_reference
        +str rule_id
        +EvaluationOutcome evaluation_result
        +str hitl_review_id
        +dict details
    }

    class LedgerEntry {
        +int sequence_num
        +str previous_hash
        +str payload_digest
        +str current_hash
        +datetime created_at
    }

    class EvaluationOutcome {
        <<enumeration>>
        PASS
        FAIL
        HITL_REVIEW
    }

    class LedgerService {
        -AsyncEngine _engine
        +append_entry(event) LedgerEntry
        -_acquire_ledger_lock(conn)
        -_last_entry(conn) tuple
    }

    class hash_chain {
        <<module>>
        +GENESIS_HASH: str
        +canonical_payload(event) str
        +compute_payload_digest(event) str
        +compute_block_hash(previous_hash, payload_digest, sequence_num, evaluated_at) str
    }

    class ChainVerificationResult {
        +bool valid
        +int entries_checked
        +list~ChainBreak~ breaks
    }

    class ChainBreak {
        +int sequence_num
        +str reason
    }

    class verify_chain {
        <<function>>
        +verify_chain(engine, start_time, end_time) ChainVerificationResult
    }

    class SingleEntryLedgerProof {
        +LedgerEntry entry
        +str previous_hash_used
        +str recomputed_current_hash
        +bool current_hash_matches
    }

    LedgerService --> ComplianceEvaluationEvent : accepts
    LedgerService --> hash_chain : computes digest + block hash via
    LedgerService --> LedgerEntry : returns
    ComplianceEvaluationEvent --> EvaluationOutcome : evaluation_result
    verify_chain --> hash_chain : recomputes via
    verify_chain --> ChainVerificationResult : returns
    ChainVerificationResult --> ChainBreak : 0..*
    verify_chain ..> LedgerEntry : reads rows as
    SingleEntryLedgerProof --> hash_chain : recomputes via
    SingleEntryLedgerProof --> LedgerEntry : wraps
```

## Supplementary — SEBI RSS Ingest Flow (Dynamic View)

```mermaid
sequenceDiagram
    autonumber
    participant SEBI as SEBI RSS/HTML Sources
    participant Poll as Ingestion Task<br/>(app.ingestion.tasks)
    participant Parse as Parser/Chunker<br/>(app.parsing)
    participant Extract as Extraction Agent<br/>(app.agents)
    participant Audit as Logic Auditor Agent<br/>(app.agents)
    participant Compile as Compiler<br/>(app.compiler)
    participant OPA as OPA Server
    participant Graph as Neo4j Knowledge Graph
    participant Vector as Qdrant
    participant Ledger as Audit Ledger (PostgreSQL)

    SEBI->>Poll: New/amended circular published
    Poll->>Poll: Deduplicate by raw_text_digest (SHA-256)
    Poll->>Parse: Layout-aware extraction (Tika/unstructured)
    Parse->>Parse: Chunk into clauses, hash each (sha256_of_clause)
    Parse->>Vector: Index clause embeddings
    Parse->>Extract: One clause at a time
    Extract->>Extract: Produce ExtractedComplianceRule + verbatim_evidence
    Extract->>Audit: Hand off for independent verification
    Audit->>Audit: Verify quotes against source, assign AuditVerdict
    alt APPROVED
        Audit->>Compile: compile_rule_to_rego + compile_rule_to_jsonlogic
        Compile->>OPA: PUT /v1/policies/{rule_id} (hot-reload)
        Compile->>Graph: Sync Circular/Clause/Obligation nodes + supersession edges
        Compile->>Ledger: Policy-compiled breach event (INFO)
    else NEEDS_REVISION
        Audit->>Extract: Re-extract (bounded, up to MAX_REVISION_ROUNDS)
    else REJECTED
        Audit->>Ledger: Route to HITLReview (Postgres) for compliance-officer review
    end
```

## Supplementary — Broker OMS / FIX Order Validation Flow (Dynamic View) [`IN PROGRESS`]

> [!NOTE]
> This sequence represents the feature-flagged FIX Gateway and native C++ policy kernel (`app/fix_gateway/`, `native/`), which is an **`IN PROGRESS`** capability decoupled from the default production REST pipeline.

```mermaid
sequenceDiagram
    autonumber
    participant OMS as Broker OMS/RMS
    participant GW as FIX Gateway [IN PROGRESS]<br/>(app.fix_gateway)
    participant Scan as FIX Tag Scanner [IN PROGRESS]<br/>(allocation-free)
    participant Kernel as Native Policy Kernel [IN PROGRESS]<br/>(native/, C++)
    participant Build as Execution Report Builder [IN PROGRESS]
    participant Async as Async Ledger Path [IN PROGRESS]<br/>(app.execution / app.ledger)
    participant Escal as Grievance Escalation [IN PROGRESS]<br/>(app.grievance_escalation)

    OMS->>GW: NewOrderSingle (35=D): ClOrdID, Account, OrderQty, Price
    GW->>Scan: scan_new_order_single(raw_bytes)
    Scan-->>GW: ParsedOrder (or ScanError -- fails closed)
    GW->>Kernel: evaluate_raw(policy, facts_vector, entity_type_hash)
    Note over Kernel: p50 ~400ns / p99 ~600ns measured<br/>(native/benchmarks/bench_fix_gateway.cpp)
    Kernel-->>GW: ALLOW or DENY (+ SEBI clause ref if DENY)
    GW->>Build: build_execution_report(order, outcome)
    Build-->>GW: Wire-format 35=8 bytes (BodyLength/CheckSum computed)
    GW->>OMS: ExecutionReport (35=8): OrdStatus, OrdRejReason (103), SebiClauseRef (9001)

    par Asynchronous, off the hot path
        GW->>Async: Transaction logged (independent of FIX response)
        Async->>Async: append_entry() -- hash-chained ledger row
        Async->>Escal: evaluate_and_trigger_grievance_escalation() if FAIL
        Escal->>Escal: check_systemic_failure() -- same broker + rule, rolling window
        alt Systemic (>= threshold within window)
            Escal->>Escal: Assemble evidence package (clause hash + payload + ledger proof)
            Escal-->>Escal: Draft grievance -- held for compliance-officer confirmation
        end
    end
```

## Diagram-to-Source Cross-Reference

| Diagram Element | Source Module | Capability Status | Live Production Path? |
|---|---|:---:|:---:|
| **Evaluator / OPAEngine / HITLQueue** | `app/execution/` | **`CURRENT`** | **Yes** |
| **Ledger Integration / hash_chain / verify_chain** | `app/ledger/` | **`CURRENT`** | **Yes** |
| **Compiler (Rego + JSON-Logic)** | `app/compiler/rego_compiler.py`, `app/compiler/jsonlogic_compiler.py` | **`CURRENT`** | **Yes** |
| **Extraction + Logic Auditor Agents** | `app/agents/crew.py` (active sequential pipeline) | **`CURRENT`** | **Yes** |
| **Ingestion / Parsing** | `app/ingestion/`, `app/parsing/` | **`CURRENT`** | **Yes** |
| **Vector Store (Clause Embeddings)** | `app/vectorstore/` | **`CURRENT`** | **Yes** |
| **Dynamic LangGraph Orchestration** | `app/agents/graph/` (feature-flagged) | **`IN PROGRESS`** | **No** (flagged: `agent_graph_orchestration_enabled=False`) |
| **FIX Gateway / Native Policy Kernel** | `app/fix_gateway/`, `native/include/regengine/` | **`IN PROGRESS`** | **No** (flagged: `fix_gateway_enabled=False`) |
| **Negotiation Orchestrator** | `app/negotiation/` | **`IN PROGRESS`** | **No** (flagged: `negotiation_enabled=False`) |
| **Canary Orchestrator** | `app/canary/` | **`IN PROGRESS`** | **No** (flagged: `canary_enabled=False`) |
| **Grievance Escalation** | `app/grievance_escalation/` | **`IN PROGRESS`** | **No** (flagged: `grievance_escalation_enabled=False`) |
| **Incident Publisher / Dashboard** | `app/incident/` | **`IN PROGRESS`** | **No** (flagged: `incident_broadcast_enabled=False`) |
| **Knowledge Graph Sync (Neo4j)** | `app/graph/` | **`IN PROGRESS`** | **No** (flagged: `neo4j_sync_enabled=False`) |
| **Zero-Knowledge Proofs (ZKP)** | `app/zkp/` | **`IN PROGRESS`** | **No** (flagged: `zkp_enabled=False`) |
| **Historical Replay Backtesting** | `app/backtest/` | **`IN PROGRESS`** | **No** (standalone batch script) |
| **QLoRA Fine-Tuning Scaffolding** | `llm_finetune/` | **`IN PROGRESS`** | **No** (standalone training pipeline) |
| **Regulatory Filing Adapter** | `app/regulatory_filing/` | **`IN PROGRESS`** | **No** (flagged: `regulatory_filing_enabled=False`) |
| **Multilingual OCR & Translation** | `app/localization/`, `translation_parity/` | **`IN PROGRESS`** | **No** (flagged: `localization_enabled=False`) |
| **Regulatory Version Diffing** | `app/diffing/` | **`IN PROGRESS`** | **No** (standalone router) |
| **Compliance Case-Law Memory Agent** | N/A | **`ROADMAP`** | **No** (proposed; `memory=False` in live pipeline) |
| **Compliance-as-Collateral Protocol** | N/A | **`ROADMAP`** | **No** (proposed) |
| **Real-Data Rule-Impact Preview** | N/A | **`ROADMAP`** | **No** (proposed) |
| **M&A Compliance Due-Diligence Agent**| N/A | **`ROADMAP`** | **No** (proposed) |
| **Fine-Tuned Domain Model (`sebi-compliance-llm`)** | N/A | **`ROADMAP`** | **No** (proposed; scaffolding in `llm_finetune/`) |
| **Ingestion Velocity Benchmark Harness** | `benchmarks/` | **`ROADMAP`** | **No** (target: `<10 min`; benchmark harness pending) |

