# ADR 002: Dual-Agent Verification Pattern for Hallucination Prevention

## Status

Accepted

## Context

RegEngine AI's compliance rules are extracted from raw SEBI circular
text by an LLM (Qwen2.5, via Hugging Face Inference, through CrewAI,
`app.agents.crew`, and an equivalent LangGraph-based orchestration in
`app.agents.graph`). An
LLM extracting a `NumericalThreshold` (e.g. "Upfront Margin >= 20%")
from legal prose can hallucinate: invent a threshold value not actually
present in the source, misattribute a threshold to the wrong entity,
or assert a scope the clause doesn't actually state. Because every
approved rule is compiled directly into an OPA policy that gates real
broker transactions (`app.compiler`, `app.execution`), a single
hallucinated threshold reaching compilation is not a cosmetic bug — it
is a wrong regulatory decision enforced against live trades.

A single LLM call's own self-reported confidence score is not a
reliable signal of hallucination: an LLM can be highly "confident" in a
fabricated value, since confidence here reflects the model's fluency at
producing a plausible-looking answer, not an independent check against
the source text.

## Decision

Every extracted rule passes through **two structurally independent LLM
roles**, not one:

1. **Extraction Agent** — reads the source clause and produces an
   `ExtractedComplianceRule` (thresholds, target entities, trigger
   conditions, obligation type), each numeric/entity claim carrying its
   own `verbatim_evidence` field: the exact source substring the agent
   claims supports that specific value.
2. **Logic Auditor Agent** — given the SAME source clause and the
   Extraction Agent's output (but not the Extraction Agent's reasoning
   trace), independently verifies every `verbatim_evidence` string
   against the source text (`app.agents.tools`'s fuzzy quote matching
   via `SequenceMatcher`, reused deterministically — not a second LLM
   call for this specific check) and produces a `ComplianceRuleAudit`:
   a hard verdict (`AuditVerdict.APPROVED` / `NEEDS_REVISION` /
   `REJECTED`), a `fidelity_score`, and zero or more `AuditFinding`
   entries typed by `FindingType` (`HALLUCINATED_THRESHOLD`,
   `HALLUCINATED_ENTITY`, `INCORRECT_ENTITY_ASSIGNMENT`,
   `UNSUPPORTED_CLAIM`, `MISCLASSIFIED_OBLIGATION`, `SCOPE_OVERREACH`,
   `UNIT_OR_VALUE_MISMATCH`, `MISSING_CONTEXT`), each tagged with a
   `Severity` (`BLOCKER` / `MAJOR` / `MINOR` / `INFO`).

**Only `APPROVED` rules ever reach `app.compiler`, the knowledge graph
(`app.graph`), or the audit ledger (`app.ledger`).** `NEEDS_REVISION`
re-enters the Extraction Agent for a bounded number of additional
rounds (`MAX_REVISION_ROUNDS = 2`, enforced identically in both
`app.agents.crew` and `app.agents.graph.nodes`) — a revision attempt
also escalates to a different model/checkpoint
(`settings.agent_fallback_model`, currently
`huggingface/Qwen/Qwen2.5-7B-Instruct`) when `extraction_confidence` falls
below `settings.agent_confidence_threshold` (0.85), rather than
re-asking the identical model the identical question. `REJECTED` (or a
revision budget exhausted with any `BLOCKER`-severity finding still
open) never auto-compiles — it routes to the HITL review queue
(`app.db.models.HITLReview`), the same "false precision is worse than
acknowledged ambiguity" posture `app.compiler.hitl` documents for every
other compiler-side ambiguity in this platform.

## Alternatives Considered

- **Single-pass extraction gated by a confidence threshold alone.**
  Rejected: as noted in Context, a self-reported confidence score is
  not an independent hallucination check — it measures the model's own
  fluency, not agreement with source text. This was the first design
  considered and explicitly rejected during this platform's build-out.
- **N-way parallel extraction with majority voting / consensus across
  independent samples.** Rejected as the PRIMARY mechanism (though a
  conceptually related pattern, weighted consensus voting, is used
  elsewhere in this platform for a different problem —
  `app.negotiation`'s multi-agent trade-compliance negotiation):
  consensus across N samples of the SAME model detects extraction
  *variance*, not hallucination against source truth specifically — N
  samples can agree on the same fabricated value if the hallucination
  is a common failure mode for that input. It is also strictly more
  expensive (N LLM calls) without producing the auditable,
  quote-verified evidence trail (`verified_quote_count` /
  `unverified_quote_count`) a regulator-facing platform needs to show
  its work.
- **Human review of every extracted clause before compilation.**
  Rejected as the default path: SEBI publishes circulars at a volume
  that makes 100% human review a throughput bottleneck this platform
  exists to remove. The dual-agent pattern is precisely what keeps
  human review (`HITLReview`) a targeted exception — reserved for
  `REJECTED`/exhausted-revision rules and specific blocking findings —
  rather than the default for every clause.
- **A single agent with a structured "self-critique" prompt (ask the
  same model to review its own output in a second turn).** Rejected:
  self-critique from the same model in the same context is far more
  prone to confirming its own prior reasoning (anchoring) than a
  genuinely separate agent invocation whose prompt is built to look for
  disagreement, not agreement.

## Consequences

- Every clause pays for at least two LLM calls (Extraction + Audit)
  instead of one, and up to `2 × (MAX_REVISION_ROUNDS + 1)` in the
  worst case (a revision round re-runs both roles) — a direct,
  accepted cost trade-off against the alternative of hallucinated
  thresholds reaching live enforcement.
- `verbatim_evidence` becomes a load-bearing field, not documentation:
  the Logic Auditor's entire hallucination-detection capability depends
  on the Extraction Agent supplying an honestly-quoted source
  substring for every claim: an agent that stops supplying evidence (or
  supplies evidence for the wrong claim) silently degrades this
  pattern's effectiveness, which is why `app.agents.tools`'s quote
  verification is a deterministic, testable function independent of
  either LLM call, not itself LLM-judged.
- The audit trail this pattern produces (`ComplianceRuleAudit`,
  `AuditFinding`) is itself a first-class artifact: it feeds
  `llm_hallucination_detection_total` (Prometheus,
  `app.observability.metrics`) and is available for a SEBI inspector to
  review why a specific rule was approved — the audit is not discarded
  once a rule passes, it is retained as evidence the platform's own
  hallucination-prevention control actually ran.
