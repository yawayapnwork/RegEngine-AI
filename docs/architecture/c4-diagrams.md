# RegEngine AI — C4 Model

Four levels (Context, Container, Component, Code) per the [C4 model](https://c4model.com/),
plus two supplementary sequence diagrams for the two flows called out
explicitly: SEBI RSS ingest, and broker OMS/FIX order validation. All
diagrams are Mermaid.js — GitHub, GitLab, and most IDE Markdown
previews render these fences natively with no extra tooling.

## Level 1 — System Context

Who and what RegEngine AI talks to, and why.

```mermaid
C4Context
    title RegEngine AI — System Context

    Person(officer, "Compliance Officer", "Reviews HITL-flagged rules/transactions, approves policy publication, confirms grievance filings")
    Person(inspector, "SEBI Inspector", "Independently re-verifies audit-log integrity offline, with no access to RegEngine's servers or database")

    System_Ext(sebiSources, "SEBI Circular Sources", "RSS feeds and HTML notice pages publishing new/amended circulars")
    System_Ext(brokerOms, "Broker OMS / RMS", "Order Management / Risk Management System submitting live orders via FIX")
    System_Ext(scores, "SEBI SCORES Portal", "Regulator grievance-redress REST API")
    System_Ext(hfInference, "Hugging Face Inference API", "Qwen2.5-72B-Instruct — dual-agent clause extraction and audit")

    System(regengine, "RegEngine AI", "Extracts, compiles, executes, and audits SEBI compliance rules against live broker transactions")

    Rel(sebiSources, regengine, "Publishes new circulars", "RSS / HTTPS")
    Rel(brokerOms, regengine, "Submits orders for validation", "FIX 4.2/4.4")
    Rel(regengine, brokerOms, "Returns Execution Reports (accept/reject + SEBI clause citation)", "FIX 4.2/4.4")
    Rel(regengine, hfInference, "Extracts & audits compliance rules from clause text", "HTTPS / Hugging Face Inference API")
    Rel(regengine, scores, "Files grievance records for systemic broker non-compliance", "HTTPS / REST")
    Rel(officer, regengine, "Reviews, approves, confirms", "HTTPS / Web UI")
    Rel(inspector, regengine, "Downloads signed audit binder (offline afterward)", "HTTPS, one-time export")
```

## Level 2 — Containers

The deployable units inside RegEngine AI's system boundary.

```mermaid
C4Container
    title RegEngine AI — Containers

    Person(officer, "Compliance Officer")
    System_Ext(sebiSources, "SEBI Circular Sources")
    System_Ext(brokerOms, "Broker OMS / RMS")
    System_Ext(scores, "SEBI SCORES Portal")
    System_Ext(hfInference, "Hugging Face Inference API")

    Container_Boundary(regengine, "RegEngine AI") {
        Container(frontend, "Compliance IDE", "React + Tailwind", "Dashboards: HITL queue, incident feed, grievance timelines, policy diffs")
        Container(api, "FastAPI Application", "Python 3.11 / FastAPI", "Synchronous REST surface: transaction evaluation, HITL, grievances, translation parity, canary control")
        Container(workers, "Celery Workers", "Python / Celery", "Async pipeline: ingestion polling, agent extraction, compilation, batch/CDC evaluation, filing/grievance submission & polling")
        Container(fixGateway, "FIX Gateway", "Python (QuickFIX) + C++", "Intercepts broker NewOrderSingle messages; validates via the native kernel; returns Execution Reports")
        Container(nativeKernel, "Native Policy Kernel", "C++17, header-only + C-ABI", "Allocation-free compiled-policy evaluator — the sub-millisecond hot path, embedded in the FIX Gateway")
        Container(opa, "OPA Server", "Open Policy Agent", "Evaluates compiled Rego policy for the general synchronous/batch/CDC path")

        ContainerDb(postgres, "PostgreSQL", "App schema + Audit Ledger", "Circulars, clauses, compiled rules, HITL reviews, and the SHA-256 hash-chained compliance_audit_ledger")
        ContainerDb(redis, "Redis", "Cache / Queue / Pub-Sub", "Celery broker, policy registry (L2), HITL/grievance/canary/negotiation queues, incident pub-sub")
        ContainerDb(qdrant, "Qdrant", "Vector Store", "Clause embeddings for semantic retrieval and hybrid Graph-RAG")
        ContainerDb(neo4j, "Neo4j", "Knowledge Graph", "Circular/Clause/Obligation/Penalty graph, supersession & conflict edges")
    }

    Rel(sebiSources, workers, "Polled by ingestion tasks", "RSS / HTTPS")
    Rel(workers, hfInference, "Extraction + Audit agent calls", "HTTPS")
    Rel(workers, opa, "Publishes compiled Rego", "HTTPS Policy API")
    Rel(workers, postgres, "Persists circulars/clauses/compiled rules")
    Rel(workers, qdrant, "Indexes clause embeddings")
    Rel(workers, neo4j, "Syncs compliance knowledge graph")

    Rel(brokerOms, fixGateway, "NewOrderSingle (35=D)", "FIX")
    Rel(fixGateway, nativeKernel, "evaluate() — in-process call")
    Rel(fixGateway, brokerOms, "ExecutionReport (35=8)", "FIX")

    Rel(api, opa, "Evaluates transactions", "HTTPS")
    Rel(api, postgres, "Reads/writes app schema + appends to audit ledger")
    Rel(api, redis, "Policy cache, HITL/grievance queues, pub-sub")
    Rel(api, scores, "Submits/polls grievances", "HTTPS")
    Rel(api, frontend, "Serves REST + WebSocket", "HTTPS/WSS")

    Rel(officer, frontend, "Uses", "HTTPS")
    Rel(nativeKernel, redis, "Hot-reloaded from", "policy_events pub-sub, via app.fix_gateway.hot_reload")
```

## Level 3 — Components (inside the FastAPI Application container)

Zooming into the container most requests actually traverse.

```mermaid
C4Component
    title RegEngine AI — Components inside the FastAPI Application

    Container_Boundary(api, "FastAPI Application") {
        Component(evaluator, "Evaluator", "app.execution.evaluator", "Reduces per-policy OPA outcomes to allow/deny/flagged (most-restrictive-wins)")
        Component(opaEngine, "OPAEngine", "app.execution.opa_engine", "Async HTTP client to the co-located OPA server; publishes and evaluates policy")
        Component(policyCache, "PolicyCache / PolicyRegistry", "app.execution.policy_cache/registry", "L1 in-process + L2 Redis view of which compiled policies apply to which entity_type")
        Component(hitlQueue, "HITLQueue", "app.execution.hitl_queue", "Redis-backed queue of ambiguous live-transaction decisions awaiting human sign-off")
        Component(ledgerIntegration, "Ledger Integration", "app.ledger.integration", "Maps one evaluation result onto hash-chained ledger rows; fires breach/grievance triggers")
        Component(killSwitch, "KillSwitchMiddleware", "app.governance.middleware", "Halts evaluation platform-wide or per-tenant on operator command")
        Component(negotiation, "Negotiation Orchestrator", "app.negotiation", "Multi-agent consensus + arbiter for cross-domain compliance conflicts")
        Component(canary, "Canary Orchestrator", "app.canary", "Shadow-evaluates a candidate policy against production traffic; auto-promotes or rolls back")
        Component(grievance, "Grievance Escalation", "app.grievance_escalation", "Detects systemic broker non-compliance; assembles evidence; files/polls SCORES")
        Component(incidentPublisher, "Incident Publisher", "app.incident.publisher", "Fans breach/grievance events out to the real-time dashboard and multi-stage escalation")
    }

    ContainerDb(opa, "OPA Server")
    ContainerDb(postgres, "PostgreSQL")
    ContainerDb(redis, "Redis")
    System_Ext(brokerOms, "Broker OMS / RMS")
    System_Ext(scores, "SEBI SCORES Portal")

    Rel(brokerOms, evaluator, "TransactionPayload", "via /v1/execution/evaluate")
    Rel(evaluator, killSwitch, "Checked before evaluating")
    Rel(evaluator, policyCache, "policies_for(entity_type)")
    Rel(evaluator, opaEngine, "evaluate(package, input_doc)")
    Rel(opaEngine, opa, "POST /v1/data/...", "HTTPS")
    Rel(evaluator, hitlQueue, "enqueue() on FLAGGED")
    Rel(evaluator, ledgerIntegration, "log_evaluation(transaction, result)")
    Rel(ledgerIntegration, postgres, "append_entry() — hash-chained insert")
    Rel(ledgerIntegration, incidentPublisher, "raise_breach_event() on FAIL/HITL_REVIEW")
    Rel(ledgerIntegration, grievance, "evaluate_and_trigger_grievance_escalation() after a successful FAIL append")
    Rel(grievance, scores, "submit / poll", "HTTPS")
    Rel(grievance, incidentPublisher, "notify_grievance_filed / _status_changed")
    Rel(negotiation, opaEngine, "Per-agent shadow evaluation")
    Rel(canary, opa, "Publishes candidate under a namespaced package")
    Rel(incidentPublisher, redis, "Redis pub-sub -> WebSocket dashboard fan-out")
```

## Level 4 — Code (the audit-ledger hash-chain module)

The most safety-critical single module in the platform (ADR 0003), at
class/function granularity.

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

## Supplementary — Broker OMS / FIX Order Validation Flow (Dynamic View)

```mermaid
sequenceDiagram
    autonumber
    participant OMS as Broker OMS/RMS
    participant GW as FIX Gateway<br/>(app.fix_gateway)
    participant Scan as FIX Tag Scanner<br/>(allocation-free)
    participant Kernel as Native Policy Kernel<br/>(native/, C++)
    participant Build as Execution Report Builder
    participant Async as Async Ledger Path<br/>(app.execution / app.ledger)
    participant Escal as Grievance Escalation<br/>(app.grievance_escalation)

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

| Diagram element | Source module |
|---|---|
| FIX Gateway / Native Policy Kernel | `app/fix_gateway/`, `native/include/regengine/` |
| Evaluator / OPAEngine / HITLQueue | `app/execution/` |
| Ledger Integration / hash_chain / verify_chain | `app/ledger/` |
| Compiler (Rego + JSON-Logic) | `app/compiler/rego_compiler.py`, `app/compiler/jsonlogic_compiler.py` |
| Extraction + Logic Auditor Agents | `app/agents/crew.py`, `app/agents/graph/` |
| Negotiation Orchestrator | `app/negotiation/` |
| Canary Orchestrator | `app/canary/` |
| Grievance Escalation | `app/grievance_escalation/` |
| Incident Publisher / Dashboard | `app/incident/` |
| Knowledge Graph sync | `app/graph/` |
| Ingestion / Parsing | `app/ingestion/`, `app/parsing/` |
