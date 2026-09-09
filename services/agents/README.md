# Agents Service

Dual-agent compliance rule extraction and logic auditing service for SEBI regulatory documents.

## Architectural Distinction: Active Pipeline vs. LangGraph Capability

### Current Production Pipeline (Active)
- **Execution Path**: Fixed two-agent sequential CrewAI pipeline (`app.agents.crew.run_dual_validation`), invoked via `app.agents.pipeline.extract_and_audit_clause`.
- **Workflow**: `Extraction Agent` produces an `ExtractedComplianceRule` &rarr; `Logic Auditor Agent` independently verifies verbatim quotes and outputs a `ComplianceRuleAudit`. If `needs_revision`, auditor findings feed back into a bounded revision loop (`MAX_REVISION_ROUNDS = 2`).
- **Active Model**: `Qwen/Qwen2.5-72B-Instruct` via Hugging Face Inference API or self-hosted endpoint (`LLM_PROVIDER=huggingface`), with deterministic regex/heuristic fallback for local and CI testing (`LLM_PROVIDER=offline`).
- **Status**: **Active in production.**

### Dynamic LangGraph Orchestration (In-Progress / Feature-Flagged)
- **Execution Path**: StateGraph state machine (`app.agents.graph.run_graph_pipeline`).
- **Workflow**: Automated regex complexity detection (`standard`, `quantitative_parsing` for math formulas, `reference_resolution` for cross-circular clauses) &rarr; confidence-gated fallback node (`huggingface/Qwen/Qwen2.5-7B-Instruct` when confidence < 0.85) &rarr; `Logic Auditor Agent` &rarr; revision loop with Redis state checkpointing (`app.agents.graph.state_store`).
- **Status**: **Implemented and unit-tested for graph topology/routing with stub nodes (`tests/test_agent_graph.py`), but currently DISABLED by default via feature flag (`settings.agent_graph_orchestration_enabled=False`). Not used in production and not verified end-to-end with live LLMs.**

## Model & Provider Architecture

- **Primary Production Model**: `Qwen/Qwen2.5-72B-Instruct` (active in live sequential CrewAI pipeline).
- **Offline / Local Mode**: Deterministic regex/heuristic rule extractor (`LLM_PROVIDER=offline`).
- **Confidence Fallback Model**: `huggingface/Qwen/Qwen2.5-7B-Instruct` (wired into the disabled LangGraph fallback node; **not active** in production while `agent_graph_orchestration_enabled=False`).
- **Provider Abstraction**: Implemented via `app.agents.providers.get_provider()` supporting `huggingface`, `offline`, `openai`, and `anthropic`. Note: Claude, GPT-4, and Llama are not executed in the live production pipeline; Hugging Face / Qwen2.5 is the configured production deployment.

Run locally: `uvicorn app.main:app --reload --port 8002`
