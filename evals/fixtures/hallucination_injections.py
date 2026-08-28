"""30 deliberately poisoned ExtractedComplianceRule objects for auditor evaluation.

Each ``HallucinationCase`` pairs a raw source clause text with a pre-built
``ExtractedComplianceRule`` that has been surgically injected with one or
more hallucinations.  The auditor agent is expected to catch every injection
and emit a finding at the correct severity.

Injection taxonomy (mirrors FindingType enum in app/agents/schemas.py)
----------------------------------------------------------------------
HT  hallucinated_threshold      Number not present in source text at all
MV  unit_or_value_mismatch      Number present but wrong value / unit / operator
HE  hallucinated_entity         Entity not named or implied in source
IE  incorrect_entity_assignment Entity present but attached to wrong obligation
MO  misclassified_obligation    Wrong obligation_type (e.g. "may" → "mandatory")
UC  unsupported_claim           verbatim_evidence is paraphrase / doesn't exist
MC  missing_context             Source qualifier / exception dropped
SO  scope_overreach             Obligation applied more broadly than source states

Coverage design
---------------
Tier 1 — Single BLOCKER injection (10 cases)
    One obvious hallucination the auditor must catch to pass.  A BLOCKER miss
    is the most dangerous failure mode (fabricated compliance rule ships to prod).

Tier 2 — Multiple injections of mixed severity (10 cases)
    Realistic LLM outputs where the model got most things right but slipped on
    edge details.  Tests precision (no false positives on clean fields) as well
    as recall (must catch all injections, not just the most obvious one).

Tier 3 — Subtle injections (10 cases)
    Hard cases: near-correct numbers (off by one decimal), plausible-but-wrong
    entities, correct number with wrong operator, and misleading verbatim_evidence
    that looks superficially like a quote but is actually a paraphrase.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.agents.schemas import (
    AuditVerdict,
    ComparisonOperator,
    ExtractedComplianceRule,
    FindingType,
    NumericalThreshold,
    ObligationType,
    QualitativeDirective,
    Severity,
    TargetEntity,
    TriggerCondition,
)


@dataclass
class ExpectedFinding:
    finding_type: FindingType
    severity: Severity
    field_path: str          # JSON-pointer into ExtractedComplianceRule
    description: str         # short human label for the eval report


@dataclass
class HallucinationCase:
    case_id: str
    tier: int                # 1, 2, or 3
    injection_summary: str   # one-line description of what was injected
    source_text: str         # the verbatim clause text the agent received
    poisoned_extraction: ExtractedComplianceRule
    expected_verdict: AuditVerdict
    expected_findings: list[ExpectedFinding]


# ---------------------------------------------------------------------------
# Helper: build a minimal valid rule (clean fields, caller injects the poison)
# ---------------------------------------------------------------------------

def _rule(
    rule_id: str,
    source_text: str,
    obligation_type: ObligationType = ObligationType.MANDATORY,
    entities: list[TargetEntity] | None = None,
    thresholds: list[NumericalThreshold] | None = None,
    triggers: list[TriggerCondition] | None = None,
    qualitative: list[QualitativeDirective] | None = None,
    confidence: float = 0.92,
) -> ExtractedComplianceRule:
    return ExtractedComplianceRule(
        rule_id=f"eval::{rule_id}",
        source_chunk_id=f"chunk::{rule_id}",
        source_sha256="a" * 64,
        circular_number="SEBI/HO/EVAL/CIR/2026/99",
        clause_number=rule_id,
        obligation_type=obligation_type,
        target_entities=entities or [],
        trigger_conditions=triggers or [],
        deterministic_logic=thresholds or [],
        qualitative_directives=qualitative or [],
        extraction_confidence=confidence,
    )


def _entity(raw: str, normalized: str, evidence: str) -> TargetEntity:
    return TargetEntity(raw_text=raw, normalized_entity=normalized, verbatim_evidence=evidence)


def _threshold(
    metric: str,
    op: ComparisonOperator,
    value: float,
    unit: str,
    evidence: str,
    applies_to: str | None = None,
    value_upper: float | None = None,
) -> NumericalThreshold:
    return NumericalThreshold(
        metric=metric,
        operator=op,
        value=value,
        value_upper=value_upper,
        unit=unit,
        applies_to=applies_to,
        verbatim_evidence=evidence,
    )


def _trigger(description: str, frequency: str | None, evidence: str) -> TriggerCondition:
    return TriggerCondition(description=description, frequency=frequency, verbatim_evidence=evidence)


def _qual(directive: str, evidence: str) -> QualitativeDirective:
    return QualitativeDirective(directive_text=directive, verbatim_evidence=evidence)


# ===========================================================================
# TIER 1 — Single BLOCKER injection (cases H-001 to H-010)
# ===========================================================================

TIER_1: list[HallucinationCase] = [

    HallucinationCase(
        case_id="H-001",
        tier=1,
        injection_summary="HT: fabricated 30% margin threshold — source says 20%.",
        source_text=(
            "Every stockbroker shall collect from its clients an upfront margin "
            "of not less than 20% of the trade value before execution of any "
            "intraday trade in equity derivatives."
        ),
        poisoned_extraction=_rule(
            "H-001", "...",
            entities=[_entity("stockbroker", "Stockbroker", "Every stockbroker shall collect")],
            thresholds=[_threshold(
                "Upfront Margin", ComparisonOperator.GTE, 30.0, "%",   # ← 30, source says 20
                "not less than 20% of the trade value",
                "intraday trade in equity derivatives",
            )],
        ),
        expected_verdict=AuditVerdict.REJECTED,
        expected_findings=[
            ExpectedFinding(FindingType.HALLUCINATED_THRESHOLD, Severity.BLOCKER,
                            "deterministic_logic[0].value",
                            "Value 30.0 not present in source; source states 20%"),
        ],
    ),

    HallucinationCase(
        case_id="H-002",
        tier=1,
        injection_summary="HE: fabricated entity 'Depository Participant' — source only mentions 'clearing member'.",
        source_text=(
            "All clearing members shall report mark-to-market losses exceeding "
            "INR 50 crore to the relevant stock exchange within 2 hours of market close."
        ),
        poisoned_extraction=_rule(
            "H-002", "...",
            entities=[
                _entity("clearing members", "Clearing Member", "All clearing members shall report"),
                _entity("Depository Participant", "Depository Participant",  # ← not in source
                        "All clearing members shall report"),
            ],
            thresholds=[
                _threshold("MTM Loss", ComparisonOperator.GT, 50.0, "INR crore",
                           "losses exceeding INR 50 crore"),
                _threshold("Reporting Deadline", ComparisonOperator.LTE, 2.0, "hours",
                           "within 2 hours of market close"),
            ],
        ),
        expected_verdict=AuditVerdict.REJECTED,
        expected_findings=[
            ExpectedFinding(FindingType.HALLUCINATED_ENTITY, Severity.BLOCKER,
                            "target_entities[1]",
                            "Depository Participant not named or implied in source clause"),
        ],
    ),

    HallucinationCase(
        case_id="H-003",
        tier=1,
        injection_summary="MO: 'may' misclassified as 'mandatory'.",
        source_text=(
            "Stock exchanges are encouraged to adopt a pre-trade risk management "
            "system that validates each order against at least five risk parameters "
            "before routing to the matching engine."
        ),
        poisoned_extraction=_rule(
            "H-003", "...",
            obligation_type=ObligationType.MANDATORY,   # ← 'encouraged' = recommended, not mandatory
            entities=[_entity("Stock exchanges", "Stock Exchange", "Stock exchanges are encouraged")],
            thresholds=[_threshold(
                "Pre-trade Risk Parameters", ComparisonOperator.GTE, 5.0, "parameters",
                "at least five risk parameters",
            )],
        ),
        expected_verdict=AuditVerdict.REJECTED,
        expected_findings=[
            ExpectedFinding(FindingType.MISCLASSIFIED_OBLIGATION, Severity.BLOCKER,
                            "obligation_type",
                            "'encouraged to adopt' should be RECOMMENDED not MANDATORY"),
        ],
    ),

    HallucinationCase(
        case_id="H-004",
        tier=1,
        injection_summary="UC: verbatim_evidence is a paraphrase, not an exact quote.",
        source_text=(
            "No Portfolio Management Service (PMS) shall hold more than 25% of "
            "its assets under management in securities issued by a single issuer."
        ),
        poisoned_extraction=_rule(
            "H-004", "...",
            obligation_type=ObligationType.PROHIBITED,
            entities=[_entity("PMS", "Portfolio Management Service",
                               "Portfolio Management Service may not hold")],  # ← paraphrase, not exact
            thresholds=[_threshold(
                "Single-Issuer Concentration", ComparisonOperator.LTE, 25.0, "%",
                "shall hold more than 25% of its assets under management",
                "AUM",
            )],
        ),
        expected_verdict=AuditVerdict.REJECTED,
        expected_findings=[
            ExpectedFinding(FindingType.UNSUPPORTED_CLAIM, Severity.BLOCKER,
                            "target_entities[0].verbatim_evidence",
                            "Evidence 'Portfolio Management Service may not hold' not found verbatim in source"),
        ],
    ),

    HallucinationCase(
        case_id="H-005",
        tier=1,
        injection_summary="HT: invented INR 100 crore net-worth figure not in source.",
        source_text=(
            "Every investment advisor registered with SEBI shall maintain a minimum "
            "net worth of INR 50 lakh at all times."
        ),
        poisoned_extraction=_rule(
            "H-005", "...",
            entities=[_entity("investment advisor", "Investment Advisor",
                               "Every investment advisor registered with SEBI")],
            thresholds=[_threshold(
                "Net Worth", ComparisonOperator.GTE, 100.0, "INR crore",  # ← should be 50 lakh
                "minimum net worth of INR 50 lakh",
            )],
        ),
        expected_verdict=AuditVerdict.REJECTED,
        expected_findings=[
            ExpectedFinding(FindingType.HALLUCINATED_THRESHOLD, Severity.BLOCKER,
                            "deterministic_logic[0].value",
                            "100.0 INR crore not present; source states 50 lakh"),
        ],
    ),

    HallucinationCase(
        case_id="H-006",
        tier=1,
        injection_summary="MO: prohibited clause flipped to mandatory.",
        source_text=(
            "No mutual fund scheme shall invest more than 30% of its net assets "
            "in securities of companies in a single sector."
        ),
        poisoned_extraction=_rule(
            "H-006", "...",
            obligation_type=ObligationType.MANDATORY,   # ← 'shall not' = prohibited
            entities=[_entity("mutual fund scheme", "Mutual Fund",
                               "No mutual fund scheme shall invest")],
            thresholds=[_threshold(
                "Single-Sector Exposure", ComparisonOperator.LTE, 30.0, "%",
                "more than 30% of its net assets",
                "net assets",
            )],
        ),
        expected_verdict=AuditVerdict.REJECTED,
        expected_findings=[
            ExpectedFinding(FindingType.MISCLASSIFIED_OBLIGATION, Severity.BLOCKER,
                            "obligation_type",
                            "'No … shall' = PROHIBITED not MANDATORY"),
        ],
    ),

    HallucinationCase(
        case_id="H-007",
        tier=1,
        injection_summary="HE: 'SEBI' listed as a TargetEntity — SEBI is regulator, not obligated party.",
        source_text=(
            "Depositories shall submit a compliance certificate to SEBI within "
            "30 days from the end of each quarter confirming adherence to the "
            "Systems Audit Framework."
        ),
        poisoned_extraction=_rule(
            "H-007", "...",
            entities=[
                _entity("Depositories", "Depository", "Depositories shall submit"),
                _entity("SEBI", "SEBI", "submit a compliance certificate to SEBI"),  # ← regulator, not entity under obligation
            ],
            thresholds=[_threshold(
                "Submission Deadline", ComparisonOperator.LTE, 30.0, "days",
                "within 30 days from the end of each quarter",
                "end of each quarter",
            )],
        ),
        expected_verdict=AuditVerdict.REJECTED,
        expected_findings=[
            ExpectedFinding(FindingType.HALLUCINATED_ENTITY, Severity.BLOCKER,
                            "target_entities[1]",
                            "SEBI is the recipient of the report, not the obligated entity"),
        ],
    ),

    HallucinationCase(
        case_id="H-008",
        tier=1,
        injection_summary="UC: evidence string for threshold contains invented text.",
        source_text=(
            "The brokerage charged by stockbrokers for equity delivery trades "
            "shall not exceed 0.5% of the trade value."
        ),
        poisoned_extraction=_rule(
            "H-008", "...",
            obligation_type=ObligationType.PROHIBITED,
            entities=[_entity("stockbrokers", "Stockbroker", "brokerage charged by stockbrokers")],
            thresholds=[_threshold(
                "Brokerage Fee", ComparisonOperator.LTE, 0.5, "%",
                "brokerage shall not exceed 0.5% of the transaction amount",  # ← 'transaction amount' not in source
                "equity delivery trade value",
            )],
        ),
        expected_verdict=AuditVerdict.REJECTED,
        expected_findings=[
            ExpectedFinding(FindingType.UNSUPPORTED_CLAIM, Severity.BLOCKER,
                            "deterministic_logic[0].verbatim_evidence",
                            "Evidence contains 'transaction amount' which does not appear verbatim in source"),
        ],
    ),

    HallucinationCase(
        case_id="H-009",
        tier=1,
        injection_summary="HT: invented 45-day offer period — source says 15 days.",
        source_text=(
            "Issuers making a rights issue shall keep the offer document open "
            "for subscription for a minimum period of 15 days."
        ),
        poisoned_extraction=_rule(
            "H-009", "...",
            entities=[_entity("Issuers", "Issuer", "Issuers making a rights issue")],
            thresholds=[_threshold(
                "Offer Document Open Period", ComparisonOperator.GTE, 45.0, "days",  # ← 45, not 15
                "minimum period of 15 days",
                "rights issue",
            )],
        ),
        expected_verdict=AuditVerdict.REJECTED,
        expected_findings=[
            ExpectedFinding(FindingType.HALLUCINATED_THRESHOLD, Severity.BLOCKER,
                            "deterministic_logic[0].value",
                            "Value 45.0 days not in source; source says minimum 15 days"),
        ],
    ),

    HallucinationCase(
        case_id="H-010",
        tier=1,
        injection_summary="SO: clause scoped to 'commodity brokers' generalised to all 'Brokers'.",
        source_text=(
            "Commodity brokers shall not allow any client's gross open position "
            "in commodity derivatives to exceed 15% of the market-wide open interest "
            "in that contract."
        ),
        poisoned_extraction=_rule(
            "H-010", "...",
            obligation_type=ObligationType.PROHIBITED,
            entities=[_entity("Brokers", "Stockbroker",  # ← over-generalised; should be Commodity Broker
                               "Commodity brokers shall not allow")],
            thresholds=[_threshold(
                "Client Gross Open Position", ComparisonOperator.LTE, 15.0, "%",
                "exceed 15% of the market-wide open interest",
                "market-wide open interest",
            )],
        ),
        expected_verdict=AuditVerdict.REJECTED,
        expected_findings=[
            ExpectedFinding(FindingType.SCOPE_OVERREACH, Severity.BLOCKER,
                            "target_entities[0].normalized_entity",
                            "Source scopes to 'commodity brokers' only; Stockbroker is over-generalisation"),
        ],
    ),
]


# ===========================================================================
# TIER 2 — Multiple injections of mixed severity (cases H-011 to H-020)
# ===========================================================================

TIER_2: list[HallucinationCase] = [

    HallucinationCase(
        case_id="H-011",
        tier=2,
        injection_summary="HT (BLOCKER) + MV (MAJOR): wrong margin value AND wrong unit on reporting threshold.",
        source_text=(
            "Stockbrokers shall levy a minimum SPAN margin of 10% for retail clients. "
            "Where aggregate margin shortfall exceeds INR 100 crore, report to SEBI "
            "within 4 hours."
        ),
        poisoned_extraction=_rule(
            "H-011", "...",
            entities=[_entity("Stockbrokers", "Stockbroker", "Stockbrokers shall levy")],
            thresholds=[
                _threshold("SPAN Margin (Retail)", ComparisonOperator.GTE, 15.0, "%",   # ← should be 10
                           "minimum SPAN margin of 10%", "retail clients"),
                _threshold("Reporting Deadline", ComparisonOperator.LTE, 4.0, "days",   # ← should be 'hours'
                           "within 4 hours"),
            ],
        ),
        expected_verdict=AuditVerdict.REJECTED,
        expected_findings=[
            ExpectedFinding(FindingType.HALLUCINATED_THRESHOLD, Severity.BLOCKER,
                            "deterministic_logic[0].value", "15.0 not in source; source says 10%"),
            ExpectedFinding(FindingType.UNIT_OR_VALUE_MISMATCH, Severity.MAJOR,
                            "deterministic_logic[1].unit", "Unit 'days' wrong; source says 'hours'"),
        ],
    ),

    HallucinationCase(
        case_id="H-012",
        tier=2,
        injection_summary="MO (BLOCKER) + MC (MAJOR): mandatory applied to optional + exception clause dropped.",
        source_text=(
            "Provided that where a client has been categorised as an institutional "
            "investor, the stockbroker may waive the upfront margin requirement of 20%; "
            "in all other cases the 20% upfront margin shall be collected mandatorily."
        ),
        poisoned_extraction=_rule(
            "H-012", "...",
            obligation_type=ObligationType.MANDATORY,   # ← correct for base case only
            entities=[_entity("stockbroker", "Stockbroker", "the stockbroker may waive")],
            thresholds=[_threshold("Upfront Margin", ComparisonOperator.GTE, 20.0, "%",
                                   "upfront margin requirement of 20%")],
        ),
        expected_verdict=AuditVerdict.REJECTED,
        expected_findings=[
            ExpectedFinding(FindingType.MISSING_CONTEXT, Severity.MAJOR,
                            "obligation_type",
                            "Institutional-investor waiver exception (conditional 'may') not reflected"),
            ExpectedFinding(FindingType.MISSING_CONTEXT, Severity.MAJOR,
                            "target_entities",
                            "Institutional Investor entity missing from extraction"),
        ],
    ),

    HallucinationCase(
        case_id="H-013",
        tier=2,
        injection_summary="HE (BLOCKER) + UC (BLOCKER): phantom entity + fabricated evidence.",
        source_text=(
            "All clearing members shall report mark-to-market losses exceeding "
            "INR 50 crore to the relevant stock exchange within 2 hours of market close."
        ),
        poisoned_extraction=_rule(
            "H-013", "...",
            entities=[
                _entity("clearing members", "Clearing Member", "All clearing members shall report"),
                _entity("trading members", "Trading Member",        # ← not in source
                        "All trading members shall report"),         # ← fabricated evidence
            ],
            thresholds=[_threshold("MTM Loss", ComparisonOperator.GT, 50.0, "INR crore",
                                   "losses exceeding INR 50 crore")],
        ),
        expected_verdict=AuditVerdict.REJECTED,
        expected_findings=[
            ExpectedFinding(FindingType.HALLUCINATED_ENTITY, Severity.BLOCKER,
                            "target_entities[1].normalized_entity",
                            "Trading Member not mentioned in source"),
            ExpectedFinding(FindingType.UNSUPPORTED_CLAIM, Severity.BLOCKER,
                            "target_entities[1].verbatim_evidence",
                            "'All trading members shall report' not in source text"),
        ],
    ),

    HallucinationCase(
        case_id="H-014",
        tier=2,
        injection_summary="MV (MAJOR) + UC (BLOCKER): wrong operator AND paraphrased evidence.",
        source_text=(
            "The portfolio duration of a liquid mutual fund scheme shall be maintained "
            "between 1 day and 91 days at all times."
        ),
        poisoned_extraction=_rule(
            "H-014", "...",
            entities=[_entity("liquid mutual fund", "Mutual Fund",
                               "liquid mutual fund scheme shall be maintained")],
            thresholds=[
                _threshold("Portfolio Duration (lower bound)", ComparisonOperator.GTE, 1.0, "days",
                           "maintained between 1 day and 91 days"),
                _threshold("Portfolio Duration (upper bound)", ComparisonOperator.GT, 91.0, "days",   # ← should be LTE
                           "duration shall not exceed ninety-one days"),  # ← paraphrase
            ],
        ),
        expected_verdict=AuditVerdict.REJECTED,
        expected_findings=[
            ExpectedFinding(FindingType.UNIT_OR_VALUE_MISMATCH, Severity.MAJOR,
                            "deterministic_logic[1].operator",
                            "Operator GT is wrong; upper bound should be LTE 91 days"),
            ExpectedFinding(FindingType.UNSUPPORTED_CLAIM, Severity.BLOCKER,
                            "deterministic_logic[1].verbatim_evidence",
                            "'shall not exceed ninety-one days' is a paraphrase, not a verbatim quote"),
        ],
    ),

    HallucinationCase(
        case_id="H-015",
        tier=2,
        injection_summary="IE (MAJOR) + MC (MAJOR): entity incorrectly assigned + dropped trigger condition.",
        source_text=(
            "Where the aggregate of all open positions of a single trading member "
            "in index futures contracts exceeds 15% of the total open interest, "
            "the trading member shall report details to the stock exchange by 9:00 PM; "
            "provided that this requirement shall not apply on expiry days."
        ),
        poisoned_extraction=_rule(
            "H-015", "...",
            obligation_type=ObligationType.MANDATORY,
            entities=[_entity("stock exchange", "Stock Exchange",    # ← obligation is on trading member
                               "the trading member shall report details to the stock exchange")],
            thresholds=[_threshold("Open Position in Index Futures", ComparisonOperator.GT, 15.0, "%",
                                   "exceeds 15% of the total open interest")],
        ),
        expected_verdict=AuditVerdict.NEEDS_REVISION,
        expected_findings=[
            ExpectedFinding(FindingType.INCORRECT_ENTITY_ASSIGNMENT, Severity.MAJOR,
                            "target_entities[0]",
                            "Obligation falls on trading member not stock exchange"),
            ExpectedFinding(FindingType.MISSING_CONTEXT, Severity.MAJOR,
                            "trigger_conditions",
                            "Expiry-day exception 'this requirement shall not apply on expiry days' not captured"),
        ],
    ),

    HallucinationCase(
        case_id="H-016",
        tier=2,
        injection_summary="HT (BLOCKER) + SO (MAJOR): wrong threshold value + scope over-extended to all AIFs.",
        source_text=(
            "All Alternative Investment Funds shall maintain a minimum corpus of "
            "INR 20 crore; Category III AIFs shall additionally maintain a minimum "
            "investable corpus of INR 5 crore at all times."
        ),
        poisoned_extraction=_rule(
            "H-016", "...",
            entities=[_entity("Alternative Investment Funds", "Alternative Investment Fund",
                               "All Alternative Investment Funds shall maintain")],
            thresholds=[
                _threshold("Minimum Corpus", ComparisonOperator.GTE, 20.0, "INR crore",
                           "minimum corpus of INR 20 crore"),
                _threshold("Minimum Investable Corpus", ComparisonOperator.GTE, 10.0, "INR crore",  # ← should be 5
                           "minimum investable corpus of INR 5 crore",
                           "all AIFs"),     # ← should be Category III only
            ],
        ),
        expected_verdict=AuditVerdict.REJECTED,
        expected_findings=[
            ExpectedFinding(FindingType.HALLUCINATED_THRESHOLD, Severity.BLOCKER,
                            "deterministic_logic[1].value",
                            "10.0 not in source; source says INR 5 crore"),
            ExpectedFinding(FindingType.SCOPE_OVERREACH, Severity.MAJOR,
                            "deterministic_logic[1].applies_to",
                            "applies_to='all AIFs' is wrong; INR 5 crore applies to Category III AIFs only"),
        ],
    ),

    HallucinationCase(
        case_id="H-017",
        tier=2,
        injection_summary="UC (BLOCKER) + MV (MAJOR): merged adjacent spans as one quote + wrong lock-in year.",
        source_text=(
            "Pre-IPO shares held by anchor investors shall be subject to a lock-in "
            "period of 90 days from the date of allotment; shares held by promoters "
            "contributing more than 20% of the post-issue paid-up capital shall be "
            "locked in for 3 years."
        ),
        poisoned_extraction=_rule(
            "H-017", "...",
            entities=[
                _entity("anchor investors", "Anchor Investor", "shares held by anchor investors shall be locked in for 90 days"),  # ← merged/paraphrased
                _entity("promoters", "Promoter", "shares held by promoters contributing more than 20%"),
            ],
            thresholds=[
                _threshold("Anchor Lock-in", ComparisonOperator.GTE, 90.0, "days",
                           "lock-in period of 90 days from the date of allotment"),
                _threshold("Promoter Lock-in", ComparisonOperator.GTE, 5.0, "years",   # ← should be 3
                           "locked in for 3 years"),
            ],
        ),
        expected_verdict=AuditVerdict.REJECTED,
        expected_findings=[
            ExpectedFinding(FindingType.UNSUPPORTED_CLAIM, Severity.BLOCKER,
                            "target_entities[0].verbatim_evidence",
                            "Evidence is a paraphrase; 'shall be locked in for 90 days' not verbatim in source"),
            ExpectedFinding(FindingType.HALLUCINATED_THRESHOLD, Severity.BLOCKER,
                            "deterministic_logic[1].value",
                            "5.0 years not in source; source says 3 years"),
        ],
    ),

    HallucinationCase(
        case_id="H-018",
        tier=2,
        injection_summary="Three MINOR findings: slightly imprecise metric names and trigger frequency.",
        source_text=(
            "Depositories shall submit a compliance certificate to SEBI within "
            "30 days from the end of each quarter confirming adherence to the "
            "Systems Audit Framework."
        ),
        poisoned_extraction=_rule(
            "H-018", "...",
            entities=[_entity("Depositories", "Depository", "Depositories shall submit")],
            thresholds=[_threshold(
                "Report Submission Window",   # ← should be "Submission Deadline" per SEBI terminology
                ComparisonOperator.LTE, 30.0, "calendar days",  # ← 'calendar' not specified; source says 'days'
                "within 30 days from the end of each quarter",
            )],
            triggers=[_trigger(
                "Quarterly Compliance Report",      # ← 'Report' vs 'Certificate'
                "semi-annual",                       # ← should be 'quarterly'
                "end of each quarter",
            )],
        ),
        expected_verdict=AuditVerdict.APPROVED,   # MINOR/INFO only — should still APPROVE
        expected_findings=[
            ExpectedFinding(FindingType.UNIT_OR_VALUE_MISMATCH, Severity.MINOR,
                            "deterministic_logic[0].unit",
                            "'calendar days' specifier not in source; source says 'days'"),
            ExpectedFinding(FindingType.UNIT_OR_VALUE_MISMATCH, Severity.MINOR,
                            "trigger_conditions[0].frequency",
                            "Frequency 'semi-annual' contradicts source 'each quarter'"),
        ],
    ),

    HallucinationCase(
        case_id="H-019",
        tier=2,
        injection_summary="MC (MAJOR) + HE (BLOCKER): missing supersession note + phantom previous circular entity.",
        source_text=(
            "In partial modification of SEBI Circular No. SEBI/HO/MRD/DP/CIR/P/2025/55, "
            "the upfront margin for options positions shall henceforth be 15% of the "
            "notional contract value (previously 12%)."
        ),
        poisoned_extraction=_rule(
            "H-019", "...",
            entities=[
                _entity("options traders", "Options Trader",   # ← entity not named in source
                        "upfront margin for options positions"),
            ],
            thresholds=[_threshold("Upfront Margin (Options)", ComparisonOperator.GTE, 15.0, "%",
                                   "henceforth be 15% of the notional contract value")],
        ),
        expected_verdict=AuditVerdict.REJECTED,
        expected_findings=[
            ExpectedFinding(FindingType.HALLUCINATED_ENTITY, Severity.BLOCKER,
                            "target_entities[0].normalized_entity",
                            "'options traders' not named in source; obligated party not explicitly stated"),
            ExpectedFinding(FindingType.MISSING_CONTEXT, Severity.MAJOR,
                            "extraction_notes",
                            "Supersession of prior circular not noted in extraction"),
        ],
    ),

    HallucinationCase(
        case_id="H-020",
        tier=2,
        injection_summary="IE (MAJOR): wrong entity assigned to one of two obligations in multi-entity clause.",
        source_text=(
            "Both the lead manager and co-manager of a public issue shall be jointly "
            "and severally liable for the accuracy of the disclosures in the offer "
            "document and shall conduct independent due diligence."
        ),
        poisoned_extraction=_rule(
            "H-020", "...",
            entities=[
                _entity("lead manager", "Lead Manager", "the lead manager and co-manager"),
                _entity("SEBI", "SEBI",    # ← SEBI is regulator, not the obligated party here
                        "shall conduct independent due diligence"),
            ],
            qualitative=[
                _qual("jointly and severally liable for accuracy of disclosures",
                      "jointly and severally liable for the accuracy of the disclosures"),
                _qual("independent due diligence",
                      "shall conduct independent due diligence"),
            ],
        ),
        expected_verdict=AuditVerdict.REJECTED,
        expected_findings=[
            ExpectedFinding(FindingType.INCORRECT_ENTITY_ASSIGNMENT, Severity.BLOCKER,
                            "target_entities[1].normalized_entity",
                            "SEBI not the obligated party; co-manager is missing"),
        ],
    ),
]


# ===========================================================================
# TIER 3 — Subtle injections (cases H-021 to H-030)
# ===========================================================================

TIER_3: list[HallucinationCase] = [

    HallucinationCase(
        case_id="H-021",
        tier=3,
        injection_summary="Subtle MV: 7.5% → 7.0% (off by 0.5 — plausible rounding).",
        source_text=(
            "The VAR margin applicable to equity securities shall be not less than "
            "7.5% for Group I securities."
        ),
        poisoned_extraction=_rule(
            "H-021", "...",
            entities=[_entity("stock exchange", "Stock Exchange",
                               "VAR margin applicable to equity securities shall be")],
            thresholds=[_threshold("VAR Margin (Group I)", ComparisonOperator.GTE, 7.0, "%",   # ← 7.0 not 7.5
                                   "not less than 7.5% for Group I securities",
                                   "Group I securities")],
        ),
        expected_verdict=AuditVerdict.REJECTED,
        expected_findings=[
            ExpectedFinding(FindingType.UNIT_OR_VALUE_MISMATCH, Severity.BLOCKER,
                            "deterministic_logic[0].value",
                            "7.0 does not match source value 7.5"),
        ],
    ),

    HallucinationCase(
        case_id="H-022",
        tier=3,
        injection_summary="Subtle UC: evidence is correct sentence but from wrong clause (copy-paste error).",
        source_text=(
            "Commodity brokers shall not allow any client's gross open position "
            "in commodity derivatives to exceed 15% of the market-wide open interest."
        ),
        poisoned_extraction=_rule(
            "H-022", "...",
            obligation_type=ObligationType.PROHIBITED,
            entities=[_entity("Commodity brokers", "Commodity Broker",
                               "No Portfolio Management Service shall hold more than 25%")],  # ← from different clause
            thresholds=[_threshold("Client Gross Open Position", ComparisonOperator.LTE, 15.0, "%",
                                   "exceed 15% of the market-wide open interest")],
        ),
        expected_verdict=AuditVerdict.REJECTED,
        expected_findings=[
            ExpectedFinding(FindingType.UNSUPPORTED_CLAIM, Severity.BLOCKER,
                            "target_entities[0].verbatim_evidence",
                            "Evidence 'No Portfolio Management Service…' does not appear in this clause's source text"),
        ],
    ),

    HallucinationCase(
        case_id="H-023",
        tier=3,
        injection_summary="Subtle HT: operator flipped from LTE to GTE for a cap rule.",
        source_text=(
            "The exit load charged by an open-ended equity scheme shall not be "
            "more than 2% of the NAV at the time of redemption."
        ),
        poisoned_extraction=_rule(
            "H-023", "...",
            obligation_type=ObligationType.PROHIBITED,
            entities=[_entity("open-ended equity scheme", "Mutual Fund",
                               "open-ended equity scheme shall not be more than 2%")],
            thresholds=[_threshold("Exit Load", ComparisonOperator.GTE, 2.0, "%",   # ← GTE is wrong; cap = LTE
                                   "not be more than 2% of the NAV",
                                   "NAV at redemption")],
        ),
        expected_verdict=AuditVerdict.REJECTED,
        expected_findings=[
            ExpectedFinding(FindingType.UNIT_OR_VALUE_MISMATCH, Severity.BLOCKER,
                            "deterministic_logic[0].operator",
                            "Operator GTE contradicts source 'shall not be MORE than'; should be LTE"),
        ],
    ),

    HallucinationCase(
        case_id="H-024",
        tier=3,
        injection_summary="Subtle SO: 'AMCs managing debt schemes' generalised to all AMCs.",
        source_text=(
            "Asset Management Companies managing debt-oriented mutual fund schemes "
            "shall conduct monthly liquidity stress tests on their portfolios."
        ),
        poisoned_extraction=_rule(
            "H-024", "...",
            obligation_type=ObligationType.MANDATORY,
            entities=[_entity("Asset Management Companies", "Asset Management Company",
                               "Asset Management Companies managing debt-oriented mutual fund schemes")],
            thresholds=[],
            triggers=[_trigger("Monthly Liquidity Stress Test", "monthly",
                                "shall conduct monthly liquidity stress tests")],
            qualitative=[_qual("monthly liquidity stress tests on portfolios",
                                "shall conduct monthly liquidity stress tests on their portfolios")],
        ),
        # The entity raw_text is correct but normalized_entity should carry the debt-scheme scoping
        # This tests whether the auditor notices scope_overreach in the normalized label
        expected_verdict=AuditVerdict.NEEDS_REVISION,
        expected_findings=[
            ExpectedFinding(FindingType.SCOPE_OVERREACH, Severity.MAJOR,
                            "target_entities[0].normalized_entity",
                            "Obligation applies only to AMCs managing debt-oriented schemes, not all AMCs"),
        ],
    ),

    HallucinationCase(
        case_id="H-025",
        tier=3,
        injection_summary="Subtle MV: % vs absolute-value unit swap (1% of NAV vs INR 1 crore).",
        source_text=(
            "A research analyst shall not hold in personal capacity more than 1% "
            "of the paid-up capital of any company it covers."
        ),
        poisoned_extraction=_rule(
            "H-025", "...",
            obligation_type=ObligationType.PROHIBITED,
            entities=[_entity("research analyst", "Research Analyst",
                               "A research analyst shall not hold")],
            thresholds=[_threshold("Personal Holding", ComparisonOperator.LTE, 1.0, "INR crore",   # ← unit wrong; should be %
                                   "more than 1% of the paid-up capital",
                                   "paid-up capital of covered company")],
        ),
        expected_verdict=AuditVerdict.REJECTED,
        expected_findings=[
            ExpectedFinding(FindingType.UNIT_OR_VALUE_MISMATCH, Severity.MAJOR,
                            "deterministic_logic[0].unit",
                            "Unit 'INR crore' wrong; source says 1% of paid-up capital"),
        ],
    ),

    HallucinationCase(
        case_id="H-026",
        tier=3,
        injection_summary="Subtle MC: cross-circular supersession context completely absent.",
        source_text=(
            "In partial modification of SEBI Circular No. SEBI/HO/IMD/DOF3/CIR/P/2024/12 "
            "dated February 1, 2024, the minimum corpus requirement for Category I AIFs "
            "is revised to INR 25 crore with immediate effect."
        ),
        poisoned_extraction=_rule(
            "H-026", "...",
            entities=[_entity("Category I AIFs", "Alternative Investment Fund",
                               "the minimum corpus requirement for Category I AIFs")],
            thresholds=[_threshold("Minimum Corpus (Cat I AIF)", ComparisonOperator.GTE, 25.0, "INR crore",
                                   "revised to INR 25 crore with immediate effect",
                                   "Category I AIF")],
            # Missing: no extraction_notes about the supersession of the 2024 circular
        ),
        expected_verdict=AuditVerdict.NEEDS_REVISION,
        expected_findings=[
            ExpectedFinding(FindingType.MISSING_CONTEXT, Severity.MAJOR,
                            "extraction_notes",
                            "Partial modification of prior circular not noted; previous value context dropped"),
        ],
    ),

    HallucinationCase(
        case_id="H-027",
        tier=3,
        injection_summary="Subtle HT: threshold for second entity tier silently dropped.",
        source_text=(
            "The daily price band shall be +/- 10% for standard securities and "
            "+/- 5% for securities under surveillance."
        ),
        poisoned_extraction=_rule(
            "H-027", "...",
            obligation_type=ObligationType.CONDITIONAL,
            entities=[_entity("Stock Exchange", "Stock Exchange",
                               "The daily price band shall be")],
            thresholds=[
                _threshold("Price Band (Standard)", ComparisonOperator.EQ, 10.0, "%",
                           "+/- 10% for standard securities", "standard securities"),
                # Surveillance band (5%) silently dropped — the agent only extracted one of two thresholds
            ],
        ),
        expected_verdict=AuditVerdict.NEEDS_REVISION,
        expected_findings=[
            ExpectedFinding(FindingType.MISSING_CONTEXT, Severity.MAJOR,
                            "deterministic_logic",
                            "Surveillance-securities band (+/- 5%) present in source but not extracted"),
        ],
    ),

    HallucinationCase(
        case_id="H-028",
        tier=3,
        injection_summary="Subtle MO: 'shall refrain' misclassified as CONDITIONAL instead of PROHIBITED.",
        source_text=(
            "Research analysts shall refrain from participating in any investment "
            "banking activity relating to an issuer whose securities they cover."
        ),
        poisoned_extraction=_rule(
            "H-028", "...",
            obligation_type=ObligationType.CONDITIONAL,   # ← should be PROHIBITED
            entities=[_entity("Research analysts", "Research Analyst",
                               "Research analysts shall refrain from")],
            qualitative=[_qual(
                "shall refrain from investment banking activity for covered issuers",
                "shall refrain from participating in any investment banking activity",
            )],
        ),
        expected_verdict=AuditVerdict.REJECTED,
        expected_findings=[
            ExpectedFinding(FindingType.MISCLASSIFIED_OBLIGATION, Severity.BLOCKER,
                            "obligation_type",
                            "'shall refrain' = PROHIBITED; CONDITIONAL requires an if/when/unless clause"),
        ],
    ),

    HallucinationCase(
        case_id="H-029",
        tier=3,
        injection_summary="Subtle UC: evidence substring is real but supporting the WRONG field value.",
        source_text=(
            "No mutual fund scheme shall invest more than 10% of its net assets in "
            "unlisted securities; provided that infrastructure debt funds may invest "
            "up to 30% in unlisted securities."
        ),
        poisoned_extraction=_rule(
            "H-029", "...",
            obligation_type=ObligationType.PROHIBITED,
            entities=[_entity("mutual fund scheme", "Mutual Fund",
                               "No mutual fund scheme shall invest more than 10%")],
            thresholds=[
                _threshold("Unlisted Securities Cap", ComparisonOperator.LTE, 10.0, "%",
                           "No mutual fund scheme shall invest more than 10% of its net assets"),
                _threshold("Infrastructure Debt Fund Cap", ComparisonOperator.LTE, 30.0, "%",
                           "No mutual fund scheme shall invest more than 10%",  # ← evidence points to 10%, not 30%
                           "infrastructure debt funds"),
            ],
        ),
        expected_verdict=AuditVerdict.REJECTED,
        expected_findings=[
            ExpectedFinding(FindingType.UNSUPPORTED_CLAIM, Severity.BLOCKER,
                            "deterministic_logic[1].verbatim_evidence",
                            "Evidence 'No mutual fund scheme…10%' supports the 10% cap, not the 30% IDF carve-out"),
        ],
    ),

    HallucinationCase(
        case_id="H-030",
        tier=3,
        injection_summary="Clean extraction — no injections. Auditor must return APPROVED with no BLOCKER findings.",
        source_text=(
            "Every stockbroker shall collect from its clients an upfront margin "
            "of not less than 20% of the trade value before execution of any "
            "intraday trade in equity derivatives."
        ),
        poisoned_extraction=_rule(
            "H-030", "...",
            obligation_type=ObligationType.MANDATORY,
            entities=[_entity("stockbroker", "Stockbroker",
                               "Every stockbroker shall collect from its clients")],
            thresholds=[_threshold(
                "Upfront Margin", ComparisonOperator.GTE, 20.0, "%",
                "not less than 20% of the trade value",
                "intraday trade in equity derivatives",
            )],
        ),
        expected_verdict=AuditVerdict.APPROVED,
        expected_findings=[],   # clean — auditor must NOT raise any BLOCKER or MAJOR findings
    ),
]


# ---------------------------------------------------------------------------
# Master injection list
# ---------------------------------------------------------------------------

ALL_INJECTIONS: list[HallucinationCase] = TIER_1 + TIER_2 + TIER_3

assert len(ALL_INJECTIONS) == 30, f"Expected 30 hallucination cases, got {len(ALL_INJECTIONS)}"


def get_injection_by_id(case_id: str) -> HallucinationCase:
    for c in ALL_INJECTIONS:
        if c.case_id == case_id:
            return c
    raise KeyError(f"No hallucination case with id={case_id!r}")


def get_injections_by_tier(tier: int) -> list[HallucinationCase]:
    return [c for c in ALL_INJECTIONS if c.tier == tier]
