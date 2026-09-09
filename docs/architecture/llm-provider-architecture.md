# LLM Provider & Model Architecture

## 1. Executive Summary

This document establishes the authoritative architecture, configuration, and runtime execution paths for Large Language Models (LLMs) across RegEngine AI.

### Clarification on Legacy Claims
Early drafts and external PRD references historically cited proprietary models (such as Claude 3.5 Sonnet) or alternative open-weight architectures (such as Llama-3-70B) as the live agentic core. **Those models are not part of the active production execution pipeline.**

The actual configured and executed production pipeline utilizes a **single-provider open-weight model strategy**:
- **Primary Model**: `Qwen/Qwen2.5-72B-Instruct` via Hugging Face Inference or self-hosted container.
- **Confidence Fallback Model**: `huggingface/Qwen/Qwen2.5-7B-Instruct` (invoked when extraction confidence drops below 0.85).
- **Offline / Deterministic Mode**: `OfflineLLMProvider` (regex and heuristic extractor for local POCs, testing, and air-gapped evaluation).
- **Pluggable Provider Abstraction**: Production-grade abstraction layer (`app.agents.providers`) supporting OpenAI and Anthropic adapters, preserved for enterprise extensibility but dormant in default deployments.

---

## 2. Actual Model & Provider Matrix

The table below delineates the exact relationship between supported providers, configured defaults, and actual production execution:

| Provider Identifier | Supported Status | Default / Configured Model | Runtime Role in Pipeline | Actually Executed in Production? | Network Target |
|---|---|---|---|:---:|---|
| `huggingface` | **Active Production** | `Qwen/Qwen2.5-72B-Instruct` (`hf_model_id`) | **Primary Extraction & Logic Audit** (dual-agent analysis) | **Yes** (when configured with `HF_TOKEN`) | Hugging Face Serverless Inference API or private TGI / vLLM endpoint |
| `huggingface` | **Active Fallback** | `Qwen/Qwen2.5-7B-Instruct` (`agent_fallback_model`) | **Low-Confidence Fallback** (invoked when confidence < 0.85 in dynamic agent graph) | **Yes** (on ambiguous / complex clauses) | Hugging Face Serverless Inference API or private TGI / vLLM endpoint |
| `offline` | **Active Development** | Deterministic Heuristic Engine (`OfflineLLMProvider`) | **Local Testing & Air-Gapped Demos** (verbatim regex quote extraction) | **Yes** (default when credentials absent) | In-process (Zero network calls) |
| `openai` | **Supported Abstraction** | `gpt-4o` (`openai_model_id`) | Alternative commercial provider adapter | **No** (dormant in standard pipeline) | OpenAI API |
| `anthropic` | **Supported Abstraction** | `claude-3-5-sonnet-20241022` (`anthropic_model_id`) | Alternative commercial provider adapter | **No** (dormant in standard pipeline) | Anthropic API |
| `sebi-compliance-llm` | **Roadmap Scaffolding** | `sebi-compliance-llm` (`llm_router_cheap_model`) | Planned self-hosted low-cost model tier (`llm_finetune/`) | **No** (scaffolding only; production training is on roadmap) | Local vLLM/Ollama (`localhost:8000/v1`) |

---

## 3. The Single-Provider Open-Weight Architecture

### Architectural Rationale
RegEngine AI standardizes on the **Qwen2.5** open-weight family for its production extraction pipeline based on deliberate architectural considerations:

1. **Deployment Flexibility & Data Sovereignty**:
   Because Qwen2.5 is an open-weight model family, deployments have the architectural choice between:
   - Serverless cloud inference during staging via Hugging Face Inference Endpoints.
   - Dedicated private hosting in a regulated financial institution's on-premises Virtual Private Cloud (VPC) using vLLM or Text Generation Inference (TGI), satisfying strict data localization requirements.
2. **Consistent Prompt & Output Geometry**:
   Utilizing `Qwen2.5-72B-Instruct` as the primary engine and `Qwen2.5-7B-Instruct` as the confidence fallback maintains structural consistency across tokenizers, chat templates, and JSON instruction adherence, reducing schema drift during model escalation.
3. **Dual-Model Confidence Cascade**:
   In the dynamic agent graph (`app.agents.graph`), clauses with extraction confidence scores below 0.85 are routed to `fallback_extraction_node`. Instead of making a redundant retry against the same 72B model, the pipeline invokes the 7B checkpoint with a fresh prompt state to obtain a diverse extraction hypothesis before presenting findings to the Logic Auditor Agent.
4. **Predictable Operational Economics**:
   Standardizing on open-weight inference avoids proprietary per-token surge pricing, rate-limit thrashing, and third-party API deprecation schedules.

---

## 4. Provider Abstraction Layer (`app/agents/providers.py`)

The codebase maintains a strict provider abstraction layer:

```
                  +--------------------------------+
                  |       get_provider()           |
                  |     (app.agents.providers)     |
                  +---------------+----------------+
                                  |
         +------------------------+------------------------+
         |                        |                        |
         v                        v                        v
+-------------------+   +--------------------+   +-------------------+
| OfflineLLMProvider|   | HuggingFaceProvider|   |   OpenAIProvider  |
|  (Deterministic)  |   | (Qwen2.5-72B / 7B) |   |  AnthropicProvider|
|  Default / Offline|   | Active Production  |   |  Dormant Adapters |
+-------------------+   +--------------------+   +-------------------+
```

### Guarantees of the Abstraction:
1. **Never Remove Abstractions for Active Providers**:
   Although Hugging Face is the active production provider, the `OpenAIProvider` and `AnthropicProvider` implementations remain fully functional to support custom deployments where enterprise policies dictate commercial API usage.
2. **Deterministic Offline Safeguard**:
   When no external API tokens (`HF_TOKEN` / `HUGGINGFACEHUB_API_TOKEN`) are configured, the pipeline gracefully defaults to `LLM_PROVIDER=offline`. The offline extractor operates deterministically, extracting rules exclusively if exact verbatim evidence can be verified against the source text.
3. **No Unaudited Downstream Emission**:
   Regardless of which provider is configured, every extraction must yield an `ExtractedComplianceRule` that is independently audited by the Logic Auditor Agent (`ComplianceRuleAudit`). Only rules receiving `AuditVerdict.APPROVED` ever proceed to OPA Rego compilation.

---

## 5. Configuration Reference

The following environment variables govern model selection in `app/config.py`:

```bash
# Provider selection: 'offline' (default), 'huggingface', 'openai', 'anthropic'
LLM_PROVIDER=huggingface

# Hugging Face Inference credentials & model specification
HUGGINGFACEHUB_API_TOKEN=hf_...
HF_MODEL_ID=Qwen/Qwen2.5-72B-Instruct

# Dynamic agent fallback model (used when extraction_confidence < 0.85)
AGENT_FALLBACK_MODEL=huggingface/Qwen/Qwen2.5-7B-Instruct
AGENT_CONFIDENCE_THRESHOLD=0.85
AGENT_MAX_FALLBACK_ATTEMPTS=2

# Concurrency bounds to protect inference endpoints from rate exhaustion
CLAUSE_CONCURRENCY=3
```
