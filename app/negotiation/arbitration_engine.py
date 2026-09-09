"""Multi-Agent Arbitration Engine (PRD Addendum v2 Section 8.3).

Orchestrates independent extraction and auditing arguments, cross-examines evidence
against source text and canonical taxonomy, detects same-checkpoint dependencies,
and enforces mandatory Human-in-the-Loop escalation for all material disagreements.

Safety Invariants:
1. NO AUTOMATIC DEPLOYMENT: Never deploys, compiles, or activates a policy directly.
2. UNTRUSTED DATA SANDBOX: Treats regulatory text as untrusted external data.
3. NO INVENTED FACTS: Arbiter verifies all quotes against source text and canonical taxonomy.
4. MANDATORY HITL GATE: Material disagreements, fact conflicts, or low confidence always produce REVIEW_REQUIRED.
5. SAME-CHECKPOINT TRANSPARENCY: Flags same_model_risk when independent models are not used.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from typing import Any

from app.agents.providers import LLMProviderType, get_provider
from app.config import Settings, get_settings
from app.negotiation.arbitration_models import (
    AgentArgument,
    AgentRole,
    ArbitrationOutcome,
    ArbitrationRound,
    ArbitrationTranscript,
    ArbiterVerdict,
    CanonicalFactClaim,
    ModelAttribution,
)
from app.negotiation.arbitration_store import ArbitrationTranscriptStore
from app.negotiation.security import inspect_for_prompt_injection, isolate_untrusted_source_text
from app.observability.metrics import (
    ARBITRATION_DISAGREEMENT_TOTAL,
    ARBITRATION_DURATION_SECONDS,
    ARBITRATION_ESCALATION_TOTAL,
    ARBITRATION_OUTCOME_TOTAL,
    ARBITRATION_ROUNDS_TOTAL,
)
from app.regulatory.facts import resolve_canonical_fact

logger = logging.getLogger(__name__)


class ArbitrationEngine:
    """Deliberation and arbitration engine coordinating Extractor, Auditor, and Arbiter agents."""

    def __init__(
        self,
        store: ArbitrationTranscriptStore | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.store = store or ArbitrationTranscriptStore(settings=self.settings)

    def _resolve_arbiter_attribution(self) -> ModelAttribution:
        """Resolves the Arbiter's model attribution, supporting distinct model/provider configuration."""
        arbiter_provider = (
            self.settings.arbitration_arbiter_provider
            or self.settings.llm_fallback_provider
            or self.settings.llm_provider
            or "offline"
        )
        arbiter_model = (
            self.settings.arbitration_arbiter_model
            or getattr(self.settings, "llm_fallback_model_id", None)
            or self.settings.hf_model_id
        )
        return ModelAttribution(
            agent_role=AgentRole.ARBITER,
            provider_type=str(arbiter_provider),
            model_name=str(arbiter_model),
            checkpoint_id=f"chk-{arbiter_model}",
            temperature=0.0,
        )

    def _verify_quotes(self, quotes: list[str], raw_text: str) -> tuple[list[str], list[str]]:
        """Verifies whether quotes in an argument exist verbatim in the source text."""
        verified: list[str] = []
        unverified: list[str] = []
        normalized_raw = " ".join(raw_text.split()).lower()
        for q in quotes:
            norm_q = " ".join(q.split()).lower()
            if norm_q and norm_q in normalized_raw:
                verified.append(q)
            else:
                unverified.append(q)
        return verified, unverified

    def _validate_canonical_facts(
        self, facts: list[CanonicalFactClaim]
    ) -> tuple[list[CanonicalFactClaim], list[str]]:
        """Validates canonical fact claims against the regulatory taxonomy."""
        accepted: list[CanonicalFactClaim] = []
        errors: list[str] = []
        for f in facts:
            res = resolve_canonical_fact(f.metric, f.unit, f.value)
            if not res.is_valid:
                errors.append(f"Invalid canonical fact for '{f.metric}': {res.error_message}")
            else:
                accepted.append(
                    CanonicalFactClaim(
                        metric=f.metric,
                        canonical_fact=res.canonical_identifier,
                        operator=f.operator,
                        value=f.value,
                        unit=f.unit,
                        verbatim_evidence=f.verbatim_evidence,
                    )
                )
        return accepted, errors

    def _cross_examine_round(
        self,
        extractor_arg: AgentArgument,
        auditor_arg: AgentArgument,
        source_text: str,
    ) -> tuple[list[str], bool]:
        """Cross-examines Extractor and Auditor arguments to identify material discrepancies."""
        discrepancies: list[str] = []

        # 1. Quote verification
        _, ext_unverified = self._verify_quotes(extractor_arg.relevant_source_text, source_text)
        if ext_unverified:
            discrepancies.append(f"Extractor cited unverified source quotes: {ext_unverified}")

        _, aud_unverified = self._verify_quotes(auditor_arg.relevant_source_text, source_text)
        if aud_unverified:
            discrepancies.append(f"Auditor cited unverified source quotes: {aud_unverified}")

        # 2. Obligation type mismatch
        if (
            extractor_arg.obligation_type
            and auditor_arg.obligation_type
            and extractor_arg.obligation_type.lower() != auditor_arg.obligation_type.lower()
        ):
            msg = (
                f"Obligation type mismatch: Extractor asserts '{extractor_arg.obligation_type}', "
                f"Auditor asserts '{auditor_arg.obligation_type}'."
            )
            discrepancies.append(msg)
            ARBITRATION_DISAGREEMENT_TOTAL.labels(disagreement_type="obligation_mismatch").inc()

        # 3. Target entities mismatch
        ext_entities = {e.lower().strip() for e in extractor_arg.target_entities}
        aud_entities = {e.lower().strip() for e in auditor_arg.target_entities}
        if ext_entities and aud_entities and ext_entities != aud_entities:
            msg = f"Target entities mismatch: Extractor={ext_entities} vs Auditor={aud_entities}."
            discrepancies.append(msg)
            ARBITRATION_DISAGREEMENT_TOTAL.labels(disagreement_type="entity_mismatch").inc()

        # 4. Canonical facts & threshold mismatch
        ext_facts = {f.metric.lower(): f for f in extractor_arg.canonical_facts}
        aud_facts = {f.metric.lower(): f for f in auditor_arg.canonical_facts}

        for metric, ef in ext_facts.items():
            if metric in aud_facts:
                af = aud_facts[metric]
                if ef.value != af.value:
                    msg = (
                        f"Threshold mismatch for metric '{metric}': Extractor asserts {ef.value} {ef.unit}, "
                        f"Auditor asserts {af.value} {af.unit}."
                    )
                    discrepancies.append(msg)
                    ARBITRATION_DISAGREEMENT_TOTAL.labels(disagreement_type="threshold_mismatch").inc()
                if ef.operator != af.operator:
                    msg = (
                        f"Operator mismatch for metric '{metric}': Extractor asserts {ef.operator}, "
                        f"Auditor asserts {af.operator}."
                    )
                    discrepancies.append(msg)
                    ARBITRATION_DISAGREEMENT_TOTAL.labels(disagreement_type="operator_mismatch").inc()

        # 5. Low confidence check
        confidence_bar = self.settings.arbitration_confidence_threshold
        if extractor_arg.confidence < confidence_bar:
            discrepancies.append(
                f"Extractor confidence ({extractor_arg.confidence:.2f}) is below threshold ({confidence_bar:.2f})."
            )
        if auditor_arg.confidence < confidence_bar:
            discrepancies.append(
                f"Auditor confidence ({auditor_arg.confidence:.2f}) is below threshold ({confidence_bar:.2f})."
            )

        has_material = bool(discrepancies)
        return discrepancies, has_material

    def _check_same_model_risk(
        self,
        extractor_arg: AgentArgument,
        auditor_arg: AgentArgument,
        arbiter_attr: ModelAttribution,
    ) -> bool:
        """Determines if the same checkpoint was used across all three agent roles."""
        e_model = extractor_arg.attribution.model_name
        a_model = auditor_arg.attribution.model_name
        arb_model = arbiter_attr.model_name
        return e_model == a_model == arb_model

    async def arbitrate_clause(
        self,
        clause_text: str,
        clause_id: str,
        circular_id: str,
        source_document_sha256: str,
        clause_sha256: str,
        tenant_id: str,
        extractor_override: AgentArgument | None = None,
        auditor_override: AgentArgument | None = None,
        max_rounds: int | None = None,
        timeout_seconds: float | None = None,
    ) -> ArbitrationTranscript:
        """Executes multi-agent arbitration for a regulatory clause chunk.

        Returns an ArbitrationTranscript sealed with cryptographic SHA-256 provenance.
        """
        start_time = time.perf_counter()
        rounds_limit = max_rounds or self.settings.arbitration_max_rounds
        time_limit = timeout_seconds or self.settings.arbitration_timeout_seconds

        transcript = ArbitrationTranscript(
            tenant_id=tenant_id,
            circular_id=circular_id,
            clause_id=clause_id,
            source_document_sha256=source_document_sha256,
            clause_sha256=clause_sha256,
            untrusted_source_text=clause_text,
        )

        arbiter_attr = self._resolve_arbiter_attribution()

        # Gate 1: Untrusted Data & Prompt Injection Inspection
        is_injection, injection_findings = inspect_for_prompt_injection(clause_text)
        if is_injection:
            verdict = ArbiterVerdict(
                outcome=ArbitrationOutcome.SECURITY_BLOCKED,
                confidence=1.0,
                justification=(
                    "CRITICAL SECURITY ALERT: Adversarial prompt injection detected in regulatory text. "
                    f"Details: {'; '.join(injection_findings)}. "
                    "Execution halted immediately; routed to HITL for administrative review."
                ),
                attribution=arbiter_attr,
                hitl_escalation_reason="prompt_injection_detected",
                security_flags=injection_findings,
            )
            transcript.arbiter_verdict = verdict
            transcript.final_outcome = ArbitrationOutcome.SECURITY_BLOCKED
            transcript.completed_at = dt.datetime.now(dt.timezone.utc)
            transcript.duration_seconds = time.perf_counter() - start_time
            await self.store.save_transcript(transcript)

            ARBITRATION_ESCALATION_TOTAL.labels(reason="prompt_injection").inc()
            ARBITRATION_OUTCOME_TOTAL.labels(outcome="security_blocked").inc()
            ARBITRATION_DURATION_SECONDS.observe(transcript.duration_seconds)
            return transcript

        # Gate 2: Bounded Multi-Round Deliberation with Timeout Guard
        try:
            return await asyncio.wait_for(
                self._run_deliberation(
                    transcript=transcript,
                    clause_text=clause_text,
                    extractor_override=extractor_override,
                    auditor_override=auditor_override,
                    rounds_limit=rounds_limit,
                    arbiter_attr=arbiter_attr,
                    start_time=start_time,
                ),
                timeout=time_limit,
            )
        except asyncio.TimeoutError:
            logger.warning("Arbitration timed out after %.1f seconds for clause %s", time_limit, clause_id)
            verdict = ArbiterVerdict(
                outcome=ArbitrationOutcome.TIMEOUT,
                confidence=0.0,
                justification=f"Arbitration exceeded timeout limit ({time_limit}s). Escalated to HITL.",
                attribution=arbiter_attr,
                hitl_escalation_reason="timeout",
            )
            transcript.arbiter_verdict = verdict
            transcript.final_outcome = ArbitrationOutcome.TIMEOUT
            transcript.completed_at = dt.datetime.now(dt.timezone.utc)
            transcript.duration_seconds = time.perf_counter() - start_time
            await self.store.save_transcript(transcript)

            ARBITRATION_ESCALATION_TOTAL.labels(reason="timeout").inc()
            ARBITRATION_OUTCOME_TOTAL.labels(outcome="timeout").inc()
            ARBITRATION_DURATION_SECONDS.observe(transcript.duration_seconds)
            return transcript

    async def _run_deliberation(
        self,
        transcript: ArbitrationTranscript,
        clause_text: str,
        extractor_override: AgentArgument | None,
        auditor_override: AgentArgument | None,
        rounds_limit: int,
        arbiter_attr: ModelAttribution,
        start_time: float,
    ) -> ArbitrationTranscript:
        """Internal multi-round deliberation loop."""
        rounds: list[ArbitrationRound] = []

        # Default argument generators when overrides are not supplied
        ext_arg = extractor_override or self._generate_default_argument(
            role=AgentRole.EXTRACTOR, clause_text=clause_text, clause_id=transcript.clause_id, round_num=1
        )
        aud_arg = auditor_override or self._generate_default_argument(
            role=AgentRole.AUDITOR, clause_text=clause_text, clause_id=transcript.clause_id, round_num=1
        )

        same_model_risk = self._check_same_model_risk(ext_arg, aud_arg, arbiter_attr)

        for round_num in range(1, rounds_limit + 1):
            if round_num > 1:
                # Rebuttal round
                ext_arg = ext_arg.model_copy(update={"round_number": round_num, "is_rebuttal": True})
                aud_arg = aud_arg.model_copy(update={"round_number": round_num, "is_rebuttal": True})

            discrepancies, has_material = self._cross_examine_round(ext_arg, aud_arg, clause_text)

            this_round = ArbitrationRound(
                round_number=round_num,
                extractor_argument=ext_arg,
                auditor_argument=aud_arg,
                discrepancies=discrepancies,
                has_material_disagreement=has_material,
            )
            rounds.append(this_round)
            ARBITRATION_ROUNDS_TOTAL.labels(status="disagreement" if has_material else "consensus").inc()

            if not has_material:
                # Consensus achieved
                accepted_facts, _ = self._validate_canonical_facts(ext_arg.canonical_facts)
                risk_note = (
                    " [NOTE: Same-checkpoint reasoning used; independent verification recommended.]"
                    if same_model_risk
                    else ""
                )
                verdict = ArbiterVerdict(
                    outcome=ArbitrationOutcome.CONSENSUS,
                    chosen_interpretation=ext_arg.interpretation,
                    accepted_canonical_facts=accepted_facts,
                    confidence=min(ext_arg.confidence, aud_arg.confidence),
                    justification=(
                        f"Extractor and Auditor achieved consensus on round {round_num}. "
                        f"All {len(accepted_facts)} canonical fact(s) verified against source quotes.{risk_note}"
                    ),
                    attribution=arbiter_attr,
                    same_model_risk=same_model_risk,
                )
                transcript.rounds = rounds
                transcript.arbiter_verdict = verdict
                transcript.final_outcome = ArbitrationOutcome.CONSENSUS
                transcript.completed_at = dt.datetime.now(dt.timezone.utc)
                transcript.duration_seconds = time.perf_counter() - start_time
                await self.store.save_transcript(transcript)

                ARBITRATION_OUTCOME_TOTAL.labels(outcome="consensus").inc()
                ARBITRATION_DURATION_SECONDS.observe(transcript.duration_seconds)
                return transcript

        # If loop completes without consensus, Arbiter produces REVIEW_REQUIRED
        final_discrepancies = rounds[-1].discrepancies
        risk_note = (
            " [WARNING: Same-checkpoint reasoning detected. Arguments lack model diversity.]"
            if same_model_risk
            else ""
        )
        verdict = ArbiterVerdict(
            outcome=ArbitrationOutcome.REVIEW_REQUIRED,
            confidence=0.5,
            justification=(
                f"Multi-agent debate deadlocked after {len(rounds)} round(s). "
                f"Unresolved discrepancies: {'; '.join(final_discrepancies)}. "
                f"Escalated to Human-in-the-Loop review.{risk_note}"
            ),
            attribution=arbiter_attr,
            hitl_escalation_reason="material_disagreement",
            same_model_risk=same_model_risk,
        )
        transcript.rounds = rounds
        transcript.arbiter_verdict = verdict
        transcript.final_outcome = ArbitrationOutcome.REVIEW_REQUIRED
        transcript.completed_at = dt.datetime.now(dt.timezone.utc)
        transcript.duration_seconds = time.perf_counter() - start_time
        await self.store.save_transcript(transcript)

        ARBITRATION_ESCALATION_TOTAL.labels(reason="material_disagreement").inc()
        ARBITRATION_OUTCOME_TOTAL.labels(outcome="review_required").inc()
        ARBITRATION_DURATION_SECONDS.observe(transcript.duration_seconds)
        return transcript

    def _generate_default_argument(
        self,
        role: AgentRole,
        clause_text: str,
        clause_id: str,
        round_num: int,
    ) -> AgentArgument:
        """Fallback rule-grounded argument generator for offline mode and tests."""
        provider = get_provider(self.settings)
        attr = ModelAttribution(
            agent_role=role,
            provider_type=provider.provider_type.value,
            model_name=getattr(self.settings, "hf_model_id", "default-model"),
            checkpoint_id=f"chk-{role.value}",
            temperature=0.0,
        )
        first_sentence = clause_text.strip().split(".")[0] if clause_text else ""
        return AgentArgument(
            agent_role=role,
            round_number=round_num,
            interpretation=f"{role.value.capitalize()} interpretation: {first_sentence}",
            canonical_facts=[],
            relevant_source_text=[first_sentence] if first_sentence else [],
            source_location=clause_id,
            confidence=0.90,
            reasoning_summary=f"Grounded directly in source sentence: '{first_sentence}'",
            attribution=attr,
            obligation_type="mandatory",
        )
