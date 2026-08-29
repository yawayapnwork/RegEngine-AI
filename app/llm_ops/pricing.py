"""Per-model token pricing, used by `app.llm_ops.cost_tracker` to convert a
raw token count into a USD figure for the cost dashboard.

Prices are USD per 1,000 tokens and must be kept in sync manually against
provider pricing pages -- there is no pricing API to poll. Update
`FRONTIER_MODEL_PRICING` when Anthropic changes list prices; the value is
deliberately NOT fetched at runtime so a pricing-endpoint outage never
breaks cost tracking (better to record slightly stale prices than none).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelPricing:
    input_per_1k_usd: float
    output_per_1k_usd: float


# Anthropic list pricing as of the model's release generation (claude-3-5-sonnet).
FRONTIER_MODEL_PRICING: dict[str, ModelPricing] = {
    "anthropic/claude-3-5-sonnet-20241022": ModelPricing(input_per_1k_usd=0.003, output_per_1k_usd=0.015),
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
