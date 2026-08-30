"""Per-model token pricing, used by `app.llm_ops.cost_tracker` to convert a
raw token count into a USD figure for the cost dashboard.

Prices are USD per 1,000 tokens and must be kept in sync manually against
provider pricing pages -- there is no pricing API to poll. Update
`FRONTIER_MODEL_PRICING` when Hugging Face's Inference Providers pricing
changes for the configured model; the value is deliberately NOT fetched
at runtime so a pricing-endpoint outage never breaks cost tracking
(better to record slightly stale prices than none).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelPricing:
    input_per_1k_usd: float
    output_per_1k_usd: float


# Hugging Face Inference Providers pricing varies by which underlying
# provider actually serves a given model (routed per-model, not a single
# HF-wide rate card) -- these figures are a placeholder estimate for
# Qwen2.5-72B-Instruct-class serverless inference and MUST be reconciled
# against the actual configured provider's billing before being trusted
# for real cost reporting.
FRONTIER_MODEL_PRICING: dict[str, ModelPricing] = {
    "huggingface/Qwen/Qwen2.5-72B-Instruct": ModelPricing(input_per_1k_usd=0.0009, output_per_1k_usd=0.0009),
}

# The "cheap" tier is a self-hosted, QLoRA-fine-tuned model (llm_finetune/)
# served locally via vLLM/Ollama -- there is no per-token API bill. The
# non-zero figure here is an amortized GPU-hour cost estimate (compute +
# power / tokens served), so the dashboard's "$ saved by routing to the
# cheap tier" number reflects a real, if approximate, marginal cost rather
# than claiming these requests are literally free.
CHEAP_TIER_PRICING: dict[str, ModelPricing] = {
    "sebi-compliance-llm": ModelPricing(input_per_1k_usd=0.0001, output_per_1k_usd=0.0002),
}

_DEFAULT_UNKNOWN_MODEL_PRICING = ModelPricing(input_per_1k_usd=0.003, output_per_1k_usd=0.015)


def get_pricing(model_name: str) -> ModelPricing:
    if model_name in FRONTIER_MODEL_PRICING:
        return FRONTIER_MODEL_PRICING[model_name]
    if model_name in CHEAP_TIER_PRICING:
        return CHEAP_TIER_PRICING[model_name]
    # Unknown model: price it as frontier-tier (conservative -- overstating
    # cost is a safer default for a cost dashboard than understating it).
    return _DEFAULT_UNKNOWN_MODEL_PRICING


def estimate_cost_usd(model_name: str, input_tokens: int, output_tokens: int) -> float:
    pricing = get_pricing(model_name)
    return (input_tokens / 1000.0) * pricing.input_per_1k_usd + (output_tokens / 1000.0) * pricing.output_per_1k_usd
