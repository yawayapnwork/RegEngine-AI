# Agents Service

Dual-agent (Extraction + Logic Auditor) compliance rule extraction via CrewAI and dynamic LangGraph orchestration.

## Model & Provider Architecture

- **Primary Production Model**: `Qwen/Qwen2.5-72B-Instruct` via Hugging Face Inference API or self-hosted container.
- **Confidence Fallback Model**: `huggingface/Qwen/Qwen2.5-7B-Instruct` (invoked when extraction confidence < 0.85 in dynamic agent routing).
- **Offline / Local Mode**: Deterministic regex/heuristic rule extractor (`LLM_PROVIDER=offline`).
- **Provider Abstraction**: Implemented via `app.agents.providers.get_provider()` supporting `huggingface`, `offline`, `openai`, and `anthropic`. Note: Claude, GPT-4, and Llama are not executed in the live production pipeline; Hugging Face / Qwen2.5 is the configured production deployment.

Run locally: `uvicorn app.main:app --reload --port 8002`
