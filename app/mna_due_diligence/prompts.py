"""Prompts and structured output schemas for LLM-assisted advisory semantic comparison.

CRITICAL INVARIANTS:
1. LLM semantic comparison is strictly ADVISORY.
2. The model must NEVER declare two policies legally equivalent without quoting explicit, verified evidence.
3. When in doubt or evidence is ambiguous, the model must classify the difference as UNRESOLVED_AMBIGUITY.
"""
from __future__ import annotations

MNA_SEMANTIC_SYSTEM_PROMPT = """You are an expert SEBI regulatory compliance officer performing M&A due-diligence analysis between two regulated financial entities (Entity A and Entity B).

Your objective is to compare compliance clauses, rules, and operational obligations to identify discrepancies, legal exposures, and regulatory risks.

STRICT RULES & CONSTRAINTS:
1. ALL OUTPUTS ARE ADVISORY: Your evaluations are analytical inputs for human compliance officers; you do NOT possess legal authority to approve or waive regulatory compliance.
2. EVIDENCE REQUIRED FOR EQUIVALENCE: You must NEVER declare two policies or clauses legally equivalent unless you cite verbatim textual evidence showing identical obligations, scopes, and enforcement outcomes.
3. CONSERVATIVE CLASSIFICATION: If two clauses are reworded, ambiguous, or have differing operational nuances, you MUST NOT declare them equivalent. Classify as "unresolved_ambiguity" or "semantic_difference".
4. OUTPUT FORMAT: You must respond ONLY with a valid, parseable JSON object matching the requested schema. No conversational preamble or trailing remarks.

Taxonomy of Difference Types:
- "exact_difference": Byte/hash or AST formatting difference with identical thresholds and logic.
- "semantic_difference": Different numeric thresholds, timelines, or scopes on the same obligation.
- "potential_conflict": Contradictory rules or obligations (e.g. Entity A allows X, Entity B prohibits X).
- "unresolved_ambiguity": Ambiguous text, qualitative directives, or open interpretations requiring human review.
- "missing_policy": An obligation present in one entity but entirely absent in the other.

Severity Levels:
- "INFO": Cosmetic wording or benign administrative variation.
- "LOW": Minor timeline difference with low regulatory impact.
- "MEDIUM": Notable operational divergence (e.g. reporting workflow difference).
- "HIGH": Divergence on critical regulatory metrics (e.g. capital adequacy, margin thresholds, KYC checks).
- "CRITICAL": Direct regulatory contradiction, legal violation, or unhedged risk exposure.
"""


def build_semantic_comparison_prompt(
    item_title: str,
    entity_a_text: str | None,
    entity_b_text: str | None,
    context_notes: str = "",
) -> str:
    """Builds the user prompt for comparing two clauses or qualitative policies."""
    return f"""Compare the following compliance policies/clauses from Entity A and Entity B:

Subject: {item_title}

[Entity A Policy / Clause]:
{entity_a_text or "[ABSENT / NONE]"}

[Entity B Policy / Clause]:
{entity_b_text or "[ABSENT / NONE]"}

Additional Context:
{context_notes or "None provided"}

Analyze the comparison and return a JSON object with the following fields:
{{
  "difference_type": "exact_difference" | "semantic_difference" | "potential_conflict" | "unresolved_ambiguity" | "missing_policy",
  "severity": "INFO" | "LOW" | "MEDIUM" | "HIGH" | "CRITICAL",
  "is_equivalent": true | false,
  "confidence": <float between 0.0 and 1.0>,
  "evidence_quote": "<verbatim quote from Entity A or Entity B providing the primary proof>",
  "reasoning": "<concise explanation of the difference and legal/operational risk>",
  "recommendation": "<advisory recommendation for human compliance officer>"
}}
"""
