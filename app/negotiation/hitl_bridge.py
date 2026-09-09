"""Bridge connecting Multi-Agent Arbitration transcripts to the Human-in-the-Loop workflow.

Guarantees:
1. FULL TRANSCRIPT PRESERVATION: Human reviewers receive the exact dialogue, quotes, and attributions.
2. SAME-CHECKPOINT TRANSPARENCY: Clearly alerts human reviewers if same model was used for all roles.
3. TAMPER AUDITABILITY: Displays the cryptographic SHA-256 seal for independent verification.
"""
from __future__ import annotations

from typing import Any

from app.negotiation.arbitration_models import ArbitrationOutcome, ArbitrationTranscript


def format_arbitration_transcript_for_hitl(transcript: ArbitrationTranscript) -> str:
    """Renders a comprehensive human-readable summary of the arbitration dialogue
    for presentation in the HITL compliance review interface.
    """
    lines: list[str] = [
        "=== MULTI-AGENT ARBITRATION TRANSCRIPT ===",
        f"Session ID: {transcript.session_id}",
        f"Tenant ID: {transcript.tenant_id}",
        f"Circular: {transcript.circular_id} | Clause: {transcript.clause_id}",
        f"Cryptographic Transcript SHA-256: {transcript.transcript_sha256}",
        f"Final Outcome: {transcript.final_outcome.value.upper()}",
        "",
    ]

    verdict = transcript.arbiter_verdict
    if verdict:
        if verdict.same_model_risk:
            lines.extend([
                "⚠️ CAUTION: SAME-CHECKPOINT REASONING DETECTED",
                "Extractor, Auditor, and Arbiter were evaluated using identical model checkpoints.",
                "Evidence does not represent genuinely independent multi-model corroboration.",
                "",
            ])
        if verdict.security_flags:
            lines.extend([
                "🚨 CRITICAL SECURITY FINDINGS:",
                *[f"• {flag}" for flag in verdict.security_flags],
                "",
            ])

        lines.extend([
            "--- ARBITER VERDICT ---",
            f"Outcome: {verdict.outcome.value}",
            f"Confidence: {verdict.confidence:.2%}",
            f"Arbiter Model: {verdict.attribution.model_name} ({verdict.attribution.provider_type})",
            f"Justification: {verdict.justification}",
            "",
        ])

    for r in transcript.rounds:
        lines.extend([
            f"--- ROUND {r.round_number} DELIBERATION ---",
            f"Material Disagreement: {r.has_material_disagreement}",
        ])
        if r.discrepancies:
            lines.append("Discrepancies:")
            for d in r.discrepancies:
                lines.append(f"  • {d}")

        lines.extend([
            "",
            f"[Extractor Argument] (Model: {r.extractor_argument.attribution.model_name}, Confidence: {r.extractor_argument.confidence:.2%})",
            f"Interpretation: {r.extractor_argument.interpretation}",
            f"Obligation Type: {r.extractor_argument.obligation_type}",
            f"Entities: {', '.join(r.extractor_argument.target_entities) or 'Unspecified'}",
            f"Source Quotes Cited: {r.extractor_argument.relevant_source_text}",
            "",
            f"[Auditor Argument] (Model: {r.auditor_argument.attribution.model_name}, Confidence: {r.auditor_argument.confidence:.2%})",
            f"Interpretation: {r.auditor_argument.interpretation}",
            f"Obligation Type: {r.auditor_argument.obligation_type}",
            f"Entities: {', '.join(r.auditor_argument.target_entities) or 'Unspecified'}",
            f"Source Quotes Cited: {r.auditor_argument.relevant_source_text}",
            "",
        ])

    return "\n".join(lines)


def should_escalate_arbitration_to_hitl(transcript: ArbitrationTranscript) -> bool:
    """Determines whether an arbitration session requires human compliance officer sign-off.

    Guarantees:
    - Never bypasses HITL on material disagreement, ambiguity, security findings, or timeouts.
    """
    if transcript.final_outcome != ArbitrationOutcome.CONSENSUS:
        return True

    verdict = transcript.arbiter_verdict
    if verdict and (verdict.security_flags or verdict.confidence < 0.85):
        return True

    return False
