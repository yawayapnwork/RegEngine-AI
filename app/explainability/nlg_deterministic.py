"""Deterministic (template-based) natural-language generation from a
`StructuredViolation`. This is the ONLY explanation path used on the hot
`POST /v1/execution/transactions/evaluate` request path (wired into
app.ledger.integration.log_evaluation) -- it is pure string formatting, no
I/O, no LLM call, so it adds no measurable latency to a path that must
"return instantly" (see app.execution.opa_engine's module docstring for
that same constraint on the OPA call itself).

The LLM fallback (app.explainability.llm_explainer) exists for messages
`trace_parser.parse_violation` cannot structurally match -- it is invoked
only from the offline/on-demand explanation path
(app.explainability.explainer.explain_evaluation_result_full,
POST /v1/explainability/explain), never from the synchronous evaluate
path.
"""
from __future__ import annotations

from app.explainability.models import ExplanationSource, LegalExplanation, StructuredViolation
from app.regulatory.taxonomy import Regulator

# The document type compliance officers actually cite in practice for
# each regulator's flagship consolidated publication -- deliberately a
# SEPARATE mapping from app.regulatory.taxonomy.RegulatorProfile.document_types
# (whose ordering instead drives which document type a *new* document's
# header text defaults to when no type name is literally present in it --
# a different, ingestion-time question this citation-label lookup must
# not couple to).
_CITATION_DOC_LABEL: dict[Regulator, str] = {
    Regulator.SEBI: "Master Circular",
    Regulator.RBI: "Master Direction",
    Regulator.IRDAI: "Regulation",
    Regulator.PFRDA: "Circular",
}

# Humanizes a compiler-facing metric name into the phrase a compliance
# officer/auditor would actually use. Falls back to the metric string
# itself (lightly re-cased) for anything not in this table -- so an
# unlisted metric still produces a grammatically valid, if slightly
# stiffer, sentence rather than failing.
_METRIC_PHRASING: dict[str, str] = {
    "Upfront Margin": "Margin collected",
    "Peak Margin": "Peak margin collected",
    "Net Worth": "Net worth",
    "Settlement Window": "Settlement timeline",
    "CRAR": "Capital to Risk-Weighted Assets Ratio (CRAR)",
    "Single Borrower Exposure": "Single-borrower exposure",
    "Solvency Ratio": "Solvency ratio",
    "Equity Exposure": "Equity exposure",
    "Leverage": "Leverage",
    "Portfolio Leverage": "Portfolio leverage",
}

# operator -> (violation_phrase_template, requirement_phrase_template).
# `{regulator}` is substituted with the regulator's acronym (SEBI/RBI/
# IRDAI/PFRDA); both phrases read naturally when combined as
# "<Metric> (<observed>) <violation_phrase> (<required>) <requirement_phrase>".
_OPERATOR_PHRASING: dict[str, tuple[str, str]] = {
    ">=": ("is below the mandatory {regulator} threshold", "required by"),
    ">": ("does not exceed the mandatory {regulator} threshold", "required by"),
    "<=": ("exceeds the maximum permitted {regulator} threshold", "permitted by"),
    "<": ("is not below the required {regulator} threshold", "required by"),
    "==": ("does not match the mandatory {regulator} requirement", "required by"),
    "range_low": ("is below the minimum permitted {regulator} threshold", "permitted by"),
    "range_high": ("exceeds the maximum permitted {regulator} threshold", "permitted by"),
}


def _format_number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def _document_type_label(regulator: Regulator) -> str:
    """Best-effort document-type phrase for the citation ("SEBI Master
    Circular", "RBI Master Direction", ...). PolicyOutcome (the OPA
    decision object's Python-side representation) does not carry the
    source document's exact DocumentType today -- only circular_number/
    clause_number -- so this uses _CITATION_DOC_LABEL's per-regulator
    default rather than leaving it blank. Extending the compiled decision
    object to also embed document_type (a small addition to
    app.compiler.rego_compiler) would let a future version of this
    function cite the exact document type instead of a default."""
    label = _CITATION_DOC_LABEL.get(regulator, "Circular")
    return f"{regulator.value.upper()} {label}"


def build_citation(violation: StructuredViolation) -> str:
    try:
        regulator = Regulator(violation.regulator)
    except ValueError:
        regulator = Regulator.SEBI
    doc_label = _document_type_label(regulator)
    clause_part = f"Clause {violation.clause_number}" if violation.clause_number else "clause unspecified"
    if violation.circular_number:
        return f"{doc_label} {clause_part} ({violation.circular_number})"
    return f"{doc_label} {clause_part}"


def build_headline(violation: StructuredViolation) -> str:
    try:
        regulator = Regulator(violation.regulator)
    except ValueError:
        regulator = Regulator.SEBI
    regulator_acronym = regulator.value.upper()

    metric_phrase = _METRIC_PHRASING.get(violation.metric, violation.metric)
    violation_phrase_tpl, requirement_verb = _OPERATOR_PHRASING.get(
        violation.operator, ("does not satisfy the mandatory {regulator} requirement", "required by")
    )
    violation_phrase = violation_phrase_tpl.format(regulator=regulator_acronym)

    observed = _format_number(violation.observed_value)
    required = _format_number(violation.required_value)
    citation = build_citation(violation)

    scope = f" for {violation.applies_to}" if violation.applies_to else ""

    return (
        f"Trade rejected: {metric_phrase} ({observed}{violation.unit}){scope} {violation_phrase} "
        f"({required}{violation.unit}) {requirement_verb} {citation}."
    )


def build_legal_explanation(violation: StructuredViolation) -> LegalExplanation:
    return LegalExplanation(
        rule_id=violation.rule_id,
        circular_number=violation.circular_number,
        clause_number=violation.clause_number,
        headline=build_headline(violation),
        citation=build_citation(violation),
        structured_violation=violation,
        source=ExplanationSource.DETERMINISTIC,
        confidence=1.0,
    )
