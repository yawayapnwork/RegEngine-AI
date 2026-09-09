"""Compliance Case-Law Memory Agent.

Safety Invariants:
1. CURRENT REGULATORY TEXT SUPREMACY: Current circular text always has priority over historical precedent.
2. NO AUTOMATIC ACTIVATION: Precedents never automatically activate policies or bypass human approval.
3. CONFLICT ESCALATION: Any conflict between current text and precedent is flagged for HITL review.
4. NO INVENTED OBLIGATIONS: Precedent must never be used to invent thresholds, deadlines, or requirements.
5. UNTRUSTED DATA BOUNDARY: Precedents are treated as untrusted historical guidance.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from app.agents.schemas import ExtractedComplianceRule
from app.case_law.models import CaseLawAnalysisResult, PrecedentMatch, PrecedentQuery
from app.case_law.store import CaseLawStore
from app.config import Settings, get_settings
from app.observability.metrics import (
    CASE_LAW_HITL_ESCALATION_TOTAL,
    CASE_LAW_RETRIEVAL_LATENCY,
    CASE_LAW_RETRIEVAL_TOTAL,
)

logger = logging.getLogger(__name__)


class CaseLawMemoryAgent:
    """Autonomous agent providing precedent-assisted context to compliance officers during HITL review."""

    def __init__(self, store: CaseLawStore | None = None, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.store = store or CaseLawStore(settings=self.settings)

    async def analyze_clause(
        self,
        clause_text: str,
        clause_number: str | None = None,
        current_rule: ExtractedComplianceRule | None = None,
        tenant_id: str = "sebi_baseline",
        top_k: int | None = None,
        min_similarity: float | None = None,
        max_age_days: int | None = None,
        allow_shared: bool = True,
    ) -> CaseLawAnalysisResult:
        """Retrieves approved historical precedents for a clause and synthesizes
        an advisory analysis strictly distinguishing primary regulatory law,
        retrieved precedent, and interpretation.
        """
        start_time = time.perf_counter()
        k = top_k or self.settings.case_law_top_k
        threshold = min_similarity if min_similarity is not None else self.settings.case_law_similarity_threshold
        age_limit = max_age_days if max_age_days is not None else self.settings.case_law_max_age_days

        query = PrecedentQuery(
            query_text=clause_text,
            tenant_id=tenant_id,
            top_k=k,
            min_similarity=threshold,
            allow_shared=allow_shared,
            max_age_days=age_limit,
        )

        try:
            matches = await self.store.search_precedents(query)
            outcome = "hit" if matches else "no_match"
        except Exception as exc:
            logger.error("Precedent search error: %s", exc)
            matches = []
            outcome = "error"

        duration = time.perf_counter() - start_time
        CASE_LAW_RETRIEVAL_LATENCY.observe(duration)
        CASE_LAW_RETRIEVAL_TOTAL.labels(outcome=outcome, tenant_id=tenant_id).inc()

        conflicts: list[str] = []
        conflict_flag_required = False

        # Inspect matches for conflicts with current regulatory text or extracted rules
        if current_rule is not None and matches:
            current_thresholds = {
                (t.metric.lower(), t.operator.value, t.value)
                for t in current_rule.deterministic_logic
            }
            for m in matches:
                prec_notes = (m.precedent.resolution_notes or "").lower()
                prec_text = m.precedent.original_clause_text.lower()
                combined_prec = f"{prec_text} {prec_notes}"
                # Check for explicit threshold differences mentioned in notes or text
                for t in current_rule.deterministic_logic:
                    metric_raw = t.metric.lower()
                    metric_spaced = metric_raw.replace("_", " ")
                    canonical_spaced = (t.canonical_fact or "").lower().replace("_", " ")
                    metric_tokens = [tok for tok in metric_spaced.split() if len(tok) > 3]

                    metric_matched = (
                        metric_raw in combined_prec
                        or metric_spaced in combined_prec
                        or (canonical_spaced and canonical_spaced in combined_prec)
                        or any(tok in combined_prec for tok in metric_tokens)
                    )

                    if metric_matched:
                        # Check if the precedent contains a different numeric quantity than the current rule
                        curr_val_str_int = str(int(t.value))
                        curr_val_str_float = f"{t.value}"
                        if curr_val_str_int not in combined_prec and curr_val_str_float not in combined_prec:
                            conflict_msg = (
                                f"Historical Precedent '{m.precedent.precedent_id}' (from Circular {m.precedent.circular_number}) "
                                f"addresses metric '{t.metric}' but differs from current circular value ({t.operator.value} {t.value} {t.unit}). "
                                "Current circular text has supremacy; human confirmation required."
                            )
                            if conflict_msg not in conflicts:
                                conflicts.append(conflict_msg)
                                conflict_flag_required = True

        if conflict_flag_required:
            CASE_LAW_HITL_ESCALATION_TOTAL.labels(reason="precedent_conflict").inc()

        # Build tri-part structured context explicitly separating source law, precedent, and guidance
        source_section = (
            "=== 1. CURRENT REGULATORY SOURCE TEXT (AUTHORITATIVE LAW) ===\n"
            f"Clause Number: {clause_number or 'Unspecified'}\n"
            f"Verbatim Text:\n\"{clause_text.strip()}\"\n"
            "NOTE: This current regulatory text is the sole authoritative basis. "
            "Historical precedents cannot override, weaken, or expand these obligations."
        )

        if matches:
            prec_blocks = []
            for i, match in enumerate(matches, start=1):
                p = match.precedent
                prec_blocks.append(
                    f"[Precedent {i}] (Cosine Similarity: {match.similarity_score:.3f})\n"
                    f"• Provenance: {match.provenance_summary}\n"
                    f"• Historical Clause: \"{p.original_clause_text.strip()}\"\n"
                    f"• Decision Outcome: {p.decision} by {p.compliance_officer_id or 'Unknown'}\n"
                    f"• Reviewer Reasoning: {p.resolution_notes or 'None recorded'}\n"
                    f"• Cryptographic Binding: Source Doc SHA-256: {p.source_document_sha256[:12]}... | "
                    f"Clause SHA-256: {p.clause_sha256[:12]}..."
                )
            precedents_section = (
                "=== 2. RETRIEVED HISTORICAL PRECEDENTS (ADVISORY GUIDANCE / UNTRUSTED HISTORICAL DATA) ===\n"
                + "\n\n".join(prec_blocks)
                + "\n\nWARNING: Precedents represent past human interpretations of earlier circulars. "
                "They are provided strictly to assist human reviewers and must never be treated as active law."
            )
        else:
            precedents_section = (
                "=== 2. RETRIEVED HISTORICAL PRECEDENTS ===\n"
                "No semantically similar approved precedents met the similarity threshold."
            )

        # Model interpretation
        if conflicts:
            interp_text = (
                "POTENTIAL CONFLICT DETECTED:\n"
                + "\n".join(f"- {c}" for c in conflicts)
                + "\n\nACTION: Current regulatory circular text supersedes historical precedent. "
                "Flagged for mandatory Compliance Officer review to establish a new governing precedent."
            )
        elif matches:
            interp_text = (
                f"Found {len(matches)} relevant approved precedent(s). The historical interpretation from "
                f"Circular {matches[0].precedent.circular_number} offers non-binding context. "
                "Verify whether the current clause's factual parameters and scope match prior interpretation."
            )
        else:
            interp_text = (
                "No prior precedents found for this clause pattern. "
                "First-principles compliance analysis required by the reviewer."
            )

        guidance = None
        if matches:
            guidance = (
                f"Consider reviewer rationale from precedent {matches[0].precedent.precedent_id}: "
                f"\"{matches[0].precedent.resolution_notes or 'Reviewed and approved'}\". "
                "Ensure current circular conditions align before adopting similar reasoning."
            )

        return CaseLawAnalysisResult(
            current_clause_text=clause_text,
            current_clause_number=clause_number,
            precedents=matches,
            has_matching_precedent=bool(matches),
            conflicts_detected=conflicts,
            conflict_flag_required=conflict_flag_required,
            current_source_text=source_section,
            historical_precedents_summary=precedents_section,
            model_interpretation=interp_text,
            reviewer_guidance=guidance,
        )
