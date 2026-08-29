"""Data contracts for the canary service. Pydantic throughout, matching
app.execution/app.negotiation conventions -- serialized to Redis via
`model_dump_json()`/`model_validate_json()` exactly like HITLCase.
"""
from __future__ import annotations

import datetime as dt
from enum import Enum

from pydantic import BaseModel, Field

from app.execution.models import Decision


class CanaryStatus(str, Enum):
    RUNNING = "running"
    PROMOTED = "promoted"
    ROLLED_BACK = "rolled_back"


class ComparisonResult(BaseModel):
    """One shadowed transaction's production-vs-candidate outcome --
    the unit `app.canary.parity.ParityAnalyzer` folds into a running
    `CanaryWindowStats`."""

    transaction_id: str
    production_decision: Decision
    candidate_decision: Decision
    production_latency_ms: float
    candidate_latency_ms: float
    production_error: str | None = None
    candidate_error: str | None = None
    compared_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))

    @property
    def diverged(self) -> bool:
        return self.production_decision != self.candidate_decision


class CanaryWindowStats(BaseModel):
    total_compared: int = 0
    matched: int = 0
    diverged: int = 0
    # decision_breakdown["<production>|<candidate>"] -> count -- a full
    # confusion matrix over {allow, deny, flagged}^2, the same shape a
    # reviewer would want from app.backtest's DeltaChangeType comparison,
    # just live rather than offline.
    decision_breakdown: dict[str, int] = Field(default_factory=dict)
    production_latency_ms_sum: float = 0.0
    candidate_latency_ms_sum: float = 0.0

    @property
    def divergence_pct(self) -> float:
        return (self.diverged / self.total_compared) if self.total_compared else 0.0

    @property
    def mean_production_latency_ms(self) -> float:
        return (self.production_latency_ms_sum / self.total_compared) if self.total_compared else 0.0

    @property
    def mean_candidate_latency_ms(self) -> float:
        return (self.candidate_latency_ms_sum / self.total_compared) if self.total_compared else 0.0


class CanaryRun(BaseModel):
    canary_id: str
    rule_id: str
    tenant_id: str | None
    production_package: str
    candidate_package: str
    candidate_opa_rule_id: str = Field(..., description="The namespaced id the candidate is actually published under in OPA (see app.canary.opa_publisher) -- never the real rule_id, so it can never collide with or shadow production's own OPA module.")
    candidate_compiled_rule_id: int = Field(..., description="app.db.models.CompiledRule.id for the candidate -- looked up at promotion time to publish it for real via PolicyPublisher.")
    status: CanaryStatus = CanaryStatus.RUNNING
    stats: CanaryWindowStats = Field(default_factory=CanaryWindowStats)
    started_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))
    resolved_at: dt.datetime | None = None
    resolution_reason: str | None = None

    def window_elapsed(self, window_seconds: int, *, now: dt.datetime | None = None) -> bool:
        now = now or dt.datetime.now(dt.timezone.utc)
        return (now - self.started_at).total_seconds() >= window_seconds


class CanaryDecision(str, Enum):
    CONTINUE = "continue"    # no spike, and either the window hasn't elapsed yet or it has but divergence is ambiguous (above the promotion bar, below the rollback bar) -- left RUNNING for a human to judge, never auto-resolved on ambiguous evidence
    PROMOTE = "promote"      # window elapsed AND divergence at or below the promotion bar
    ROLLBACK = "rollback"    # divergence at or above the rollback bar, checked after every comparison regardless of window elapsed
