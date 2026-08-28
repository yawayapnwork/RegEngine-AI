"""50 ground-truth SEBI circular benchmark scenarios for the extraction agent.

Coverage matrix
---------------
Category A — Simple numeric thresholds (10 scenarios)
    Straightforward margin/exposure rules with a single numeric threshold,
    one entity, and clear modal-verb obligation type.

Category B — Nested / compound conditions (10 scenarios)
    Multi-clause conditions linked by "subject to", "provided that", "except
    where", nested if/then/else logic, and conditional obligation chains.

Category C — Conflicting or ambiguous thresholds (10 scenarios)
    Same metric with two different values in the same clause (deliberate
    SEBI revision language), range thresholds, floor/ceiling pairs that
    appear to contradict, and cross-references to Annexures.

Category D — Unstructured table data (10 scenarios)
    Numeric rules embedded in prose-described tables, multi-column threshold
    matrices (entity_type × threshold × unit), and percentage staircase
    schedules.

Category E — Edge cases and qualitative-only clauses (10 scenarios)
    Pure qualitative directives with no numeric content, cross-circular
    references ("as per Circular No. SEBI/…/2024"), ambiguous entity scope,
    multi-entity clauses, and clauses where the obligation is conditional on
    an external regulatory event.

Ground-truth format
-------------------
Each scenario is a ``ScenarioFixture`` with:
  ``scenario_id``       Unique stable identifier (used for regression tracking)
  ``category``          One of A–E above
  ``description``       Human-readable summary of what makes this scenario notable
  ``clause_text``       The raw source text the extraction agent receives
  ``circular_number``   SEBI circular reference (fabricated but realistic)
  ``clause_number``     Clause/section reference
  ``ground_truth``      ``GroundTruth`` with expected extraction fields

``GroundTruth`` intentionally mirrors ``ExtractedComplianceRule`` at the
field level so metrics code can do field-by-field comparison without special
casing.  Fields are omitted (None / empty list) when a category doesn't
exercise them — metrics code treats missing GT fields as "not evaluated".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Ground-truth data models (independent of app schemas — no crewai import)
# ---------------------------------------------------------------------------

@dataclass
class GTThreshold:
    metric: str
    operator: str        # ">=", ">", "<=", "<", "==", "range"
    value: float
    value_upper: Optional[float]
    unit: str
    applies_to: Optional[str]


@dataclass
class GTEntity:
    normalized_entity: str   # canonical SEBI taxonomy name


@dataclass
class GTTrigger:
    description: str
    frequency: Optional[str]


@dataclass
class GTQualitative:
    directive_text: str


@dataclass
class GroundTruth:
    obligation_type: str                        # mandatory/prohibited/conditional/recommended
    entities: list[GTEntity]                    = field(default_factory=list)
    thresholds: list[GTThreshold]               = field(default_factory=list)
    triggers: list[GTTrigger]                   = field(default_factory=list)
    qualitative_directives: list[GTQualitative] = field(default_factory=list)
    # Spans that MUST appear in ambiguous_spans (not forced into structure)
    expected_ambiguous_fragments: list[str]     = field(default_factory=list)
    # Extraction should produce HITL flag for these reason codes
    expected_hitl_flags: list[str]              = field(default_factory=list)


@dataclass
class ScenarioFixture:
    scenario_id: str
    category: str        # "A" through "E"
    description: str
    clause_text: str
    circular_number: str
    clause_number: str
    ground_truth: GroundTruth


# ---------------------------------------------------------------------------
# Category A — Simple numeric thresholds
# ---------------------------------------------------------------------------

CAT_A: list[ScenarioFixture] = [
    ScenarioFixture(
        scenario_id="A-001",
        category="A",
        description="Standard upfront margin floor for stockbrokers.",
        clause_text=(
            "Every stockbroker shall collect from its clients an upfront margin "
            "of not less than 20% of the trade value before execution of any "
            "intraday trade in equity derivatives."
        ),
        circular_number="SEBI/HO/MRD/DP/CIR/P/2026/01",
        clause_number="4.1",
        ground_truth=GroundTruth(
            obligation_type="mandatory",
            entities=[GTEntity("Stockbroker")],
            thresholds=[GTThreshold("Upfront Margin", ">=", 20.0, None, "%", "intraday trade in equity derivatives")],
        ),
    ),
    ScenarioFixture(
        scenario_id="A-002",
        category="A",
        description="Daily MTM loss reporting deadline for clearing members.",
        clause_text=(
            "All clearing members shall report mark-to-market losses exceeding "
            "INR 50 crore to the relevant stock exchange within 2 hours of "
            "market close on each trading day."
        ),
        circular_number="SEBI/HO/MRD/DP/CIR/P/2026/02",
        clause_number="5.2",
        ground_truth=GroundTruth(
            obligation_type="mandatory",
            entities=[GTEntity("Clearing Member")],
            thresholds=[
                GTThreshold("MTM Loss", ">", 50.0, None, "INR crore", None),
                GTThreshold("Reporting Deadline", "<=", 2.0, None, "hours", None),
            ],
            triggers=[GTTrigger("Daily MTM Loss Reporting", "daily")],
        ),
    ),
    ScenarioFixture(
        scenario_id="A-003",
        category="A",
        description="Prohibition on concentration above single-issuer exposure cap.",
        clause_text=(
            "No Portfolio Management Service (PMS) shall hold more than 25% of "
            "its assets under management in securities issued by a single issuer."
        ),
        circular_number="SEBI/HO/IMD/DF1/CIR/P/2026/03",
        clause_number="3.1",
        ground_truth=GroundTruth(
            obligation_type="prohibited",
            entities=[GTEntity("Portfolio Management Service")],
            thresholds=[GTThreshold("Single-Issuer Concentration", "<=", 25.0, None, "%", "AUM")],
        ),
    ),
    ScenarioFixture(
        scenario_id="A-004",
        category="A",
        description="Minimum net worth requirement for SEBI-registered investment advisors.",
        clause_text=(
            "Every investment advisor registered with SEBI shall maintain a minimum "
            "net worth of INR 50 lakh at all times."
        ),
        circular_number="SEBI/HO/IMD/IMD-I/DOF1/CIR/P/2026/04",
        clause_number="6.1",
        ground_truth=GroundTruth(
            obligation_type="mandatory",
            entities=[GTEntity("Investment Advisor")],
            thresholds=[GTThreshold("Net Worth", ">=", 50.0, None, "INR lakh", None)],
        ),
    ),
    ScenarioFixture(
        scenario_id="A-005",
        category="A",
        description="T+1 settlement obligation for equity cash market trades.",
        clause_text=(
            "Stock exchanges shall ensure settlement of all equity cash market "
            "transactions within 1 working day of the trade date (T+1)."
        ),
        circular_number="SEBI/HO/MRD/MRD-PoD-1/CIR/P/2026/05",
        clause_number="2.3",
        ground_truth=GroundTruth(
            obligation_type="mandatory",
            entities=[GTEntity("Stock Exchange")],
            thresholds=[GTThreshold("Settlement Cycle", "<=", 1.0, None, "working day", None)],
        ),
    ),
    ScenarioFixture(
        scenario_id="A-006",
        category="A",
        description="Maximum brokerage fee cap for equity delivery trades.",
        clause_text=(
            "The brokerage charged by stockbrokers for equity delivery trades "
            "shall not exceed 0.5% of the trade value."
        ),
        circular_number="SEBI/HO/MRD/DP/CIR/P/2026/06",
        clause_number="7.4",
        ground_truth=GroundTruth(
            obligation_type="prohibited",
            entities=[GTEntity("Stockbroker")],
            thresholds=[GTThreshold("Brokerage Fee", "<=", 0.5, None, "%", "equity delivery trade value")],
        ),
    ),
    ScenarioFixture(
        scenario_id="A-007",
        category="A",
        description="Minimum disclosure period for rights issue offer documents.",
        clause_text=(
            "Issuers making a rights issue shall keep the offer document open "
            "for subscription for a minimum period of 15 days."
        ),
        circular_number="SEBI/HO/CFD/DIL1/CIR/P/2026/07",
        clause_number="8.2",
        ground_truth=GroundTruth(
            obligation_type="mandatory",
            entities=[GTEntity("Issuer")],
            thresholds=[GTThreshold("Offer Document Open Period", ">=", 15.0, None, "days", "rights issue")],
        ),
    ),
    ScenarioFixture(
        scenario_id="A-008",
        category="A",
        description="Mutual fund scheme-level exposure limit to a single sector.",
        clause_text=(
            "No mutual fund scheme shall invest more than 30% of its net assets "
            "in securities of companies in a single sector."
        ),
        circular_number="SEBI/HO/IMD/DOF3/CIR/P/2026/08",
        clause_number="5.1",
        ground_truth=GroundTruth(
            obligation_type="prohibited",
            entities=[GTEntity("Mutual Fund")],
            thresholds=[GTThreshold("Single-Sector Exposure", "<=", 30.0, None, "%", "net assets")],
        ),
    ),
    ScenarioFixture(
        scenario_id="A-009",
        category="A",
        description="Quarterly compliance certificate submission for depositories.",
        clause_text=(
            "Depositories shall submit a compliance certificate to SEBI within "
            "30 days from the end of each quarter confirming adherence to the "
            "Systems Audit Framework."
        ),
        circular_number="SEBI/HO/MRD/MRD-PoD-3/CIR/P/2026/09",
        clause_number="4.5",
        ground_truth=GroundTruth(
            obligation_type="mandatory",
            entities=[GTEntity("Depository")],
            thresholds=[GTThreshold("Submission Deadline", "<=", 30.0, None, "days", "end of each quarter")],
            triggers=[GTTrigger("Quarterly Compliance Certificate Submission", "quarterly")],
        ),
    ),
    ScenarioFixture(
        scenario_id="A-010",
        category="A",
        description="Maximum exposure to a single counterparty for commodity brokers.",
        clause_text=(
            "Commodity brokers shall not allow any client's gross open position "
            "in commodity derivatives to exceed 15% of the market-wide open interest "
            "in that contract."
        ),
        circular_number="SEBI/HO/CDMRD/DMP/CIR/P/2026/10",
        clause_number="6.3",
        ground_truth=GroundTruth(
            obligation_type="prohibited",
            entities=[GTEntity("Commodity Broker")],
            thresholds=[GTThreshold("Client Gross Open Position", "<=", 15.0, None, "%", "market-wide open interest")],
        ),
    ),
]


# ---------------------------------------------------------------------------
# Category B — Nested / compound conditions
# ---------------------------------------------------------------------------

CAT_B: list[ScenarioFixture] = [
    ScenarioFixture(
        scenario_id="B-001",
        category="B",
        description="Margin waiver conditioned on client risk profile AND trade type.",
        clause_text=(
            "Provided that where a client has been categorised as an institutional "
            "investor as defined under Regulation 2(1)(zd) of the SEBI (ICDR) "
            "Regulations and the trade is in government securities, the stockbroker "
            "may waive the upfront margin requirement of 20%; in all other cases "
            "the 20% upfront margin shall be collected mandatorily."
        ),
        circular_number="SEBI/HO/MRD/DP/CIR/P/2026/11",
        clause_number="4.2",
        ground_truth=GroundTruth(
            obligation_type="conditional",
            entities=[GTEntity("Stockbroker"), GTEntity("Institutional Investor")],
            thresholds=[GTThreshold("Upfront Margin", ">=", 20.0, None, "%", None)],
        ),
    ),
    ScenarioFixture(
        scenario_id="B-002",
        category="B",
        description="Nested short-selling restriction: broker AND client conditions.",
        clause_text=(
            "No stockbroker shall facilitate a short sale in any security unless: "
            "(i) the stockbroker has a valid stock lending and borrowing agreement "
            "in place; (ii) the client has furnished a declaration in Form SB-1 "
            "confirming availability of the security; and (iii) the security is "
            "listed on a recognised stock exchange with a minimum free-float "
            "market capitalisation of INR 500 crore."
        ),
        circular_number="SEBI/HO/MRD/DP/CIR/P/2026/12",
        clause_number="3.4",
        ground_truth=GroundTruth(
            obligation_type="prohibited",
            entities=[GTEntity("Stockbroker")],
            thresholds=[GTThreshold("Free-Float Market Capitalisation", ">=", 500.0, None, "INR crore", None)],
        ),
    ),
    ScenarioFixture(
        scenario_id="B-003",
        category="B",
        description="Conditional reporting obligation triggered by breach of threshold.",
        clause_text=(
            "Where the aggregate of all open positions of a single trading member "
            "in index futures contracts exceeds 15% of the total open interest in "
            "such contracts on any given day, the trading member shall report the "
            "details of such positions to the stock exchange by 9:00 PM on that day; "
            "provided that this requirement shall not apply on expiry days."
        ),
        circular_number="SEBI/HO/MRD/DP/CIR/P/2026/13",
        clause_number="5.1",
        ground_truth=GroundTruth(
            obligation_type="conditional",
            entities=[GTEntity("Trading Member")],
            thresholds=[GTThreshold("Open Position in Index Futures", ">", 15.0, None, "%", "total open interest")],
            triggers=[GTTrigger("Breach-triggered Position Reporting", "daily")],
        ),
    ),
    ScenarioFixture(
        scenario_id="B-004",
        category="B",
        description="Tiered margin obligation based on client segment.",
        clause_text=(
            "Stockbrokers shall levy the following SPAN margins: (a) for retail "
            "clients, not less than 10% of the contract value; (b) for non-retail "
            "clients with a net worth below INR 10 crore, not less than 8%; and "
            "(c) for non-retail clients with a net worth of INR 10 crore or above, "
            "not less than 5%."
        ),
        circular_number="SEBI/HO/MRD/DP/CIR/P/2026/14",
        clause_number="4.3",
        ground_truth=GroundTruth(
            obligation_type="mandatory",
            entities=[GTEntity("Stockbroker")],
            thresholds=[
                GTThreshold("SPAN Margin (Retail)", ">=", 10.0, None, "%", "retail clients"),
                GTThreshold("SPAN Margin (Non-Retail, NW < 10 Cr)", ">=", 8.0, None, "%", "non-retail clients NW < 10 crore"),
                GTThreshold("SPAN Margin (Non-Retail, NW >= 10 Cr)", ">=", 5.0, None, "%", "non-retail clients NW >= 10 crore"),
            ],
        ),
    ),
    ScenarioFixture(
        scenario_id="B-005",
        category="B",
        description="Exception clause: margin waiver for sovereign guarantees.",
        clause_text=(
            "The exposure margin of 5% applicable to equity index futures shall "
            "not be required where the client's open position is fully backed by "
            "a bank guarantee issued by a scheduled commercial bank rated AA or "
            "above by a SEBI-registered credit rating agency; in all other cases "
            "the exposure margin shall be collected in full."
        ),
        circular_number="SEBI/HO/MRD/DP/CIR/P/2026/15",
        clause_number="4.6",
        ground_truth=GroundTruth(
            obligation_type="conditional",
            entities=[GTEntity("Stockbroker")],
            thresholds=[GTThreshold("Exposure Margin", ">=", 5.0, None, "%", "equity index futures")],
        ),
    ),
    ScenarioFixture(
        scenario_id="B-006",
        category="B",
        description="Multi-step escalation: breach triggers internal review, then SEBI report.",
        clause_text=(
            "In the event of a margin shortfall, the stockbroker shall: (i) "
            "immediately issue a margin call to the client; (ii) if the shortfall "
            "is not rectified within 1 trading day, square off sufficient open "
            "positions to cover the shortfall; and (iii) if the aggregate shortfall "
            "across all clients exceeds INR 100 crore on any day, report the same "
            "to SEBI within 4 hours."
        ),
        circular_number="SEBI/HO/MRD/DP/CIR/P/2026/16",
        clause_number="6.2",
        ground_truth=GroundTruth(
            obligation_type="mandatory",
            entities=[GTEntity("Stockbroker")],
            thresholds=[
                GTThreshold("Margin Shortfall Rectification Window", "<=", 1.0, None, "trading day", None),
                GTThreshold("Aggregate Shortfall Reporting Trigger", ">", 100.0, None, "INR crore", None),
                GTThreshold("SEBI Reporting Deadline", "<=", 4.0, None, "hours", None),
            ],
        ),
    ),
    ScenarioFixture(
        scenario_id="B-007",
        category="B",
        description="Condition referencing an external schedule via cross-reference.",
        clause_text=(
            "The risk management framework for algorithmic trading shall comply "
            "with the minimum standards specified in Schedule III to this circular, "
            "provided that where the algorithmic trading system generates more than "
            "500 orders per second, additional controls as specified in Schedule IV "
            "shall also apply."
        ),
        circular_number="SEBI/HO/MRD/DP/CIR/P/2026/17",
        clause_number="7.1",
        ground_truth=GroundTruth(
            obligation_type="conditional",
            entities=[GTEntity("Trading Member")],
            thresholds=[GTThreshold("Order Generation Rate", ">", 500.0, None, "orders per second", None)],
            expected_ambiguous_fragments=["Schedule III", "Schedule IV"],
        ),
    ),
    ScenarioFixture(
        scenario_id="B-008",
        category="B",
        description="Subject-to clause with regulatory override.",
        clause_text=(
            "Subject to any direction issued by SEBI from time to time, all "
            "Alternative Investment Funds shall maintain a minimum corpus of "
            "INR 20 crore throughout the tenure of the fund; Category III AIFs "
            "shall additionally maintain a minimum investable corpus of INR 5 crore "
            "at all times."
        ),
        circular_number="SEBI/HO/IMD/DF6/CIR/P/2026/18",
        clause_number="3.2",
        ground_truth=GroundTruth(
            obligation_type="mandatory",
            entities=[GTEntity("Alternative Investment Fund")],
            thresholds=[
                GTThreshold("Minimum Corpus", ">=", 20.0, None, "INR crore", "all AIFs"),
                GTThreshold("Minimum Investable Corpus", ">=", 5.0, None, "INR crore", "Category III AIF"),
            ],
        ),
    ),
    ScenarioFixture(
        scenario_id="B-009",
        category="B",
        description="Intraday position limit with sunset provision.",
        clause_text=(
            "The intraday trading limit for any stockbroker's proprietary account "
            "shall not exceed 10 times its net worth; provided that for a period of "
            "90 days from the date of this circular, a transitional limit of 15 "
            "times net worth shall apply, after which the 10 times limit shall "
            "be in force."
        ),
        circular_number="SEBI/HO/MRD/DP/CIR/P/2026/19",
        clause_number="5.5",
        ground_truth=GroundTruth(
            obligation_type="conditional",
            entities=[GTEntity("Stockbroker")],
            thresholds=[
                GTThreshold("Intraday Proprietary Trading Limit", "<=", 10.0, None, "x net worth", "post-transition"),
                GTThreshold("Transitional Intraday Limit", "<=", 15.0, None, "x net worth", "first 90 days"),
                GTThreshold("Transition Period", "<=", 90.0, None, "days", None),
            ],
        ),
    ),
    ScenarioFixture(
        scenario_id="B-010",
        category="B",
        description="Dual-condition lock-in for pre-IPO shares.",
        clause_text=(
            "Pre-IPO shares held by anchor investors shall be subject to a lock-in "
            "period of 90 days from the date of allotment; shares held by promoters "
            "contributing more than 20% of the post-issue paid-up capital shall be "
            "locked in for 3 years, and the balance promoter holding shall be locked "
            "in for 1 year."
        ),
        circular_number="SEBI/HO/CFD/DIL1/CIR/P/2026/20",
        clause_number="9.3",
        ground_truth=GroundTruth(
            obligation_type="mandatory",
            entities=[GTEntity("Issuer"), GTEntity("Anchor Investor"), GTEntity("Promoter")],
            thresholds=[
                GTThreshold("Anchor Investor Lock-in", ">=", 90.0, None, "days", "anchor investors"),
                GTThreshold("Promoter Lock-in (>20% holding)", ">=", 3.0, None, "years", "promoters with >20% post-issue capital"),
                GTThreshold("Promoter Contribution Threshold", ">", 20.0, None, "%", "post-issue paid-up capital"),
                GTThreshold("Balance Promoter Lock-in", ">=", 1.0, None, "year", "balance promoter holding"),
            ],
        ),
    ),
]


# ---------------------------------------------------------------------------
# Category C — Conflicting or ambiguous thresholds
# ---------------------------------------------------------------------------

CAT_C: list[ScenarioFixture] = [
    ScenarioFixture(
        scenario_id="C-001",
        category="C",
        description="Same metric appears with two different values in one clause.",
        clause_text=(
            "The daily price band for equity shares shall ordinarily be +/- 10%; "
            "however, where the Exchange has identified a security as being under "
            "surveillance, the price band shall be reduced to +/- 5% until further "
            "notice from the Exchange."
        ),
        circular_number="SEBI/HO/MRD/MRD-PoD-3/CIR/P/2026/21",
        clause_number="2.1",
        ground_truth=GroundTruth(
            obligation_type="conditional",
            entities=[GTEntity("Stock Exchange")],
            thresholds=[
                GTThreshold("Price Band (Standard)", "==", 10.0, None, "%", "ordinary securities"),
                GTThreshold("Price Band (Surveillance)", "==", 5.0, None, "%", "securities under surveillance"),
            ],
        ),
    ),
    ScenarioFixture(
        scenario_id="C-002",
        category="C",
        description="Explicit range threshold: between two values.",
        clause_text=(
            "The portfolio duration of a liquid mutual fund scheme shall be maintained "
            "between 1 day and 91 days at all times."
        ),
        circular_number="SEBI/HO/IMD/DOF3/CIR/P/2026/22",
        clause_number="4.2",
        ground_truth=GroundTruth(
            obligation_type="mandatory",
            entities=[GTEntity("Mutual Fund")],
            thresholds=[GTThreshold("Portfolio Duration", "range", 1.0, 91.0, "days", "liquid fund scheme")],
        ),
    ),
    ScenarioFixture(
        scenario_id="C-003",
        category="C",
        description="Two thresholds for same metric with different operator directions.",
        clause_text=(
            "No single debt mutual fund scheme shall hold less than 75% of its net "
            "assets in debt instruments rated AA or above; nor shall it hold more "
            "than 10% in unrated instruments."
        ),
        circular_number="SEBI/HO/IMD/DOF3/CIR/P/2026/23",
        clause_number="5.4",
        ground_truth=GroundTruth(
            obligation_type="mandatory",
            entities=[GTEntity("Mutual Fund")],
            thresholds=[
                GTThreshold("AA-rated Debt Allocation", ">=", 75.0, None, "%", "net assets"),
                GTThreshold("Unrated Instrument Allocation", "<=", 10.0, None, "%", "net assets"),
            ],
        ),
    ),
    ScenarioFixture(
        scenario_id="C-004",
        category="C",
        description="Threshold defined by cross-reference to an Annexure — no inline number.",
        clause_text=(
            "The haircut applicable to government securities accepted as collateral "
            "shall be as specified in Annexure A of this circular, which shall be "
            "reviewed and updated by SEBI on a quarterly basis."
        ),
        circular_number="SEBI/HO/MRD/DP/CIR/P/2026/24",
        clause_number="3.7",
        ground_truth=GroundTruth(
            obligation_type="mandatory",
            entities=[GTEntity("Clearing Corporation")],
            thresholds=[],   # no inline numeric — must NOT be extracted as a threshold
            expected_ambiguous_fragments=["Annexure A"],
            expected_hitl_flags=["qualitative_directive"],
        ),
    ),
    ScenarioFixture(
        scenario_id="C-005",
        category="C",
        description="Supersession language: new value overrides prior circular.",
        clause_text=(
            "In partial modification of SEBI Circular No. SEBI/HO/MRD/DP/CIR/P/2025/55 "
            "dated January 15, 2025, the upfront margin for options positions shall "
            "henceforth be 15% of the notional contract value (previously 12%)."
        ),
        circular_number="SEBI/HO/MRD/DP/CIR/P/2026/25",
        clause_number="1.2",
        ground_truth=GroundTruth(
            obligation_type="mandatory",
            entities=[GTEntity("Stockbroker")],
            # Only the new (current) value should be extracted; 12% is historical
            thresholds=[GTThreshold("Upfront Margin (Options)", ">=", 15.0, None, "%", "notional contract value")],
        ),
    ),
    ScenarioFixture(
        scenario_id="C-006",
        category="C",
        description="Floor and ceiling defined in separate sub-clauses of one sentence.",
        clause_text=(
            "The exit load charged by an open-ended equity scheme shall not be "
            "less than 0.5% nor more than 2% of the NAV at the time of redemption, "
            "for redemptions made within 1 year of investment."
        ),
        circular_number="SEBI/HO/IMD/DOF3/CIR/P/2026/26",
        clause_number="6.1",
        ground_truth=GroundTruth(
            obligation_type="mandatory",
            entities=[GTEntity("Mutual Fund")],
            thresholds=[
                GTThreshold("Exit Load", "range", 0.5, 2.0, "%", "NAV at redemption"),
                GTThreshold("Redemption Period", "<=", 1.0, None, "year", None),
            ],
        ),
    ),
    ScenarioFixture(
        scenario_id="C-007",
        category="C",
        description="Percentage AND absolute value both stated as alternatives.",
        clause_text=(
            "A research analyst shall not hold in personal capacity more than 1% "
            "of the paid-up capital of any company it covers, or INR 1 crore in "
            "market value, whichever is lower."
        ),
        circular_number="SEBI/HO/DAFN/DAFN-RA/P/CIR/2026/27",
        clause_number="5.3",
        ground_truth=GroundTruth(
            obligation_type="prohibited",
            entities=[GTEntity("Research Analyst")],
            thresholds=[
                GTThreshold("Personal Holding (% cap)", "<=", 1.0, None, "%", "paid-up capital of covered company"),
                GTThreshold("Personal Holding (absolute cap)", "<=", 1.0, None, "INR crore", "market value"),
            ],
        ),
    ),
    ScenarioFixture(
        scenario_id="C-008",
        category="C",
        description="Staircase margin schedule embedded in a single run-on clause.",
        clause_text=(
            "The VAR margin applicable to equity securities shall be computed as "
            "follows: for securities in Group I, VAR margin shall be not less than "
            "7.5%; for Group II securities, not less than 8.5%; and for Group III "
            "securities, not less than 10%."
        ),
        circular_number="SEBI/HO/MRD/DP/CIR/P/2026/28",
        clause_number="4.4",
        ground_truth=GroundTruth(
            obligation_type="mandatory",
            entities=[GTEntity("Stock Exchange"), GTEntity("Clearing Corporation")],
            thresholds=[
                GTThreshold("VAR Margin (Group I)", ">=", 7.5, None, "%", "Group I securities"),
                GTThreshold("VAR Margin (Group II)", ">=", 8.5, None, "%", "Group II securities"),
                GTThreshold("VAR Margin (Group III)", ">=", 10.0, None, "%", "Group III securities"),
            ],
        ),
    ),
    ScenarioFixture(
        scenario_id="C-009",
        category="C",
        description="Contradictory-seeming limits resolved by entity scoping.",
        clause_text=(
            "Custodians acting on behalf of Foreign Portfolio Investors shall "
            "maintain a minimum liquid asset buffer of 5% of custodial assets; "
            "custodians acting on behalf of domestic institutional investors shall "
            "maintain a buffer of 3% of custodial assets."
        ),
        circular_number="SEBI/HO/MRD/DP/CIR/P/2026/29",
        clause_number="7.2",
        ground_truth=GroundTruth(
            obligation_type="mandatory",
            entities=[GTEntity("Custodian"), GTEntity("Foreign Portfolio Investor"), GTEntity("Domestic Institutional Investor")],
            thresholds=[
                GTThreshold("Liquid Asset Buffer (FPI custodians)", ">=", 5.0, None, "%", "custodial assets for FPIs"),
                GTThreshold("Liquid Asset Buffer (DII custodians)", ">=", 3.0, None, "%", "custodial assets for DIIs"),
            ],
        ),
    ),
    ScenarioFixture(
        scenario_id="C-010",
        category="C",
        description="Threshold defined via formula — extraction must surface as qualitative.",
        clause_text=(
            "The initial margin requirement for stock futures shall be the higher of: "
            "(a) the SPAN-computed margin; or (b) 5% of the notional value of the "
            "open position; computed at the end-of-day mark-to-market settlement price."
        ),
        circular_number="SEBI/HO/MRD/DP/CIR/P/2026/30",
        clause_number="4.7",
        ground_truth=GroundTruth(
            obligation_type="mandatory",
            entities=[GTEntity("Clearing Corporation")],
            thresholds=[GTThreshold("Floor Margin (5% Notional)", ">=", 5.0, None, "%", "notional value of open position")],
            expected_ambiguous_fragments=["SPAN-computed margin"],
        ),
    ),
]


# ---------------------------------------------------------------------------
# Category D — Unstructured table data
# ---------------------------------------------------------------------------

CAT_D: list[ScenarioFixture] = [
    ScenarioFixture(
        scenario_id="D-001",
        category="D",
        description="Prose-rendered two-column margin table for index derivatives.",
        clause_text=(
            "The following margins are applicable to index derivative contracts: "
            "NIFTY 50 Futures — 5.0%; NIFTY Bank Futures — 6.0%; NIFTY IT Futures "
            "— 5.5%; NIFTY Midcap 150 Futures — 7.0%."
        ),
        circular_number="SEBI/HO/MRD/DP/CIR/P/2026/31",
        clause_number="3.1",
        ground_truth=GroundTruth(
            obligation_type="mandatory",
            entities=[GTEntity("Stock Exchange"), GTEntity("Clearing Corporation")],
            thresholds=[
                GTThreshold("NIFTY 50 Futures Margin", ">=", 5.0, None, "%", "NIFTY 50 Futures"),
                GTThreshold("NIFTY Bank Futures Margin", ">=", 6.0, None, "%", "NIFTY Bank Futures"),
                GTThreshold("NIFTY IT Futures Margin", ">=", 5.5, None, "%", "NIFTY IT Futures"),
                GTThreshold("NIFTY Midcap 150 Futures Margin", ">=", 7.0, None, "%", "NIFTY Midcap 150 Futures"),
            ],
        ),
    ),
    ScenarioFixture(
        scenario_id="D-002",
        category="D",
        description="Investor category × product × limit matrix in prose form.",
        clause_text=(
            "The following investment limits are prescribed: (i) Retail Individual "
            "Investor in Sovereign Gold Bonds — maximum INR 4 lakh per financial year; "
            "(ii) HUF in Sovereign Gold Bonds — maximum INR 4 lakh per financial year; "
            "(iii) Trust in Sovereign Gold Bonds — maximum INR 20 lakh per financial year."
        ),
        circular_number="SEBI/HO/IMD/DOF3/CIR/P/2026/32",
        clause_number="5.2",
        ground_truth=GroundTruth(
            obligation_type="prohibited",
            entities=[GTEntity("Retail Individual Investor"), GTEntity("HUF"), GTEntity("Trust")],
            thresholds=[
                GTThreshold("SGB Investment Limit (Retail/HUF)", "<=", 4.0, None, "INR lakh", "per financial year"),
                GTThreshold("SGB Investment Limit (Trust)", "<=", 20.0, None, "INR lakh", "per financial year"),
            ],
        ),
    ),
    ScenarioFixture(
        scenario_id="D-003",
        category="D",
        description="Time-based staircase penalty schedule.",
        clause_text=(
            "Late submission penalties for annual reports shall be as follows: "
            "delay of 1–30 days — INR 1,000 per day; delay of 31–60 days — "
            "INR 5,000 per day; delay exceeding 60 days — INR 10,000 per day "
            "plus referral to adjudication."
        ),
        circular_number="SEBI/HO/CFD/DIL1/CIR/P/2026/33",
        clause_number="8.1",
        ground_truth=GroundTruth(
            obligation_type="mandatory",
            entities=[GTEntity("Listed Entity")],
            thresholds=[
                GTThreshold("Late Penalty (1-30 days)", "==", 1000.0, None, "INR per day", "delay 1–30 days"),
                GTThreshold("Late Penalty (31-60 days)", "==", 5000.0, None, "INR per day", "delay 31–60 days"),
                GTThreshold("Late Penalty (>60 days)", "==", 10000.0, None, "INR per day", "delay > 60 days"),
            ],
        ),
    ),
    ScenarioFixture(
        scenario_id="D-004",
        category="D",
        description="Multi-entity × threshold matrix for credit exposure limits.",
        clause_text=(
            "Maximum credit exposure limits per counterparty: scheduled commercial "
            "banks — 25% of net worth; co-operative banks — 15% of net worth; "
            "NBFCs rated AAA — 20% of net worth; NBFCs rated below AA — 10% of net worth."
        ),
        circular_number="SEBI/HO/IMD/DOF3/CIR/P/2026/34",
        clause_number="6.4",
        ground_truth=GroundTruth(
            obligation_type="prohibited",
            entities=[GTEntity("Mutual Fund"), GTEntity("Scheduled Commercial Bank"), GTEntity("Co-operative Bank"), GTEntity("NBFC")],
            thresholds=[
                GTThreshold("Counterparty Exposure (SCB)", "<=", 25.0, None, "%", "net worth — scheduled commercial banks"),
                GTThreshold("Counterparty Exposure (Co-op Bank)", "<=", 15.0, None, "%", "net worth — co-operative banks"),
                GTThreshold("Counterparty Exposure (AAA NBFC)", "<=", 20.0, None, "%", "net worth — AAA-rated NBFCs"),
                GTThreshold("Counterparty Exposure (Below AA NBFC)", "<=", 10.0, None, "%", "net worth — NBFCs rated below AA"),
            ],
        ),
    ),
    ScenarioFixture(
        scenario_id="D-005",
        category="D",
        description="Option expiry schedule embedded as comma-separated day list.",
        clause_text=(
            "Weekly options on indices shall expire on every Tuesday, Wednesday, "
            "and Friday of the week; monthly options shall expire on the last "
            "Thursday of each month; and quarterly options shall expire on the "
            "last Thursday of March, June, September, and December."
        ),
        circular_number="SEBI/HO/MRD/DP/CIR/P/2026/35",
        clause_number="2.4",
        ground_truth=GroundTruth(
            obligation_type="mandatory",
            entities=[GTEntity("Stock Exchange")],
            thresholds=[],
            triggers=[
                GTTrigger("Weekly Options Expiry", "weekly (Tue/Wed/Fri)"),
                GTTrigger("Monthly Options Expiry", "monthly (last Thursday)"),
                GTTrigger("Quarterly Options Expiry", "quarterly (last Thursday of Mar/Jun/Sep/Dec)"),
            ],
        ),
    ),
    ScenarioFixture(
        scenario_id="D-006",
        category="D",
        description="Percentage matrix with both floor and cap in same cell.",
        clause_text=(
            "The expense ratio for direct plans of mutual fund schemes shall be: "
            "equity-oriented schemes — minimum 0.10%, maximum 1.05%; debt-oriented "
            "schemes — minimum 0.10%, maximum 0.80%; hybrid schemes — minimum 0.10%, "
            "maximum 1.00%."
        ),
        circular_number="SEBI/HO/IMD/DOF3/CIR/P/2026/36",
        clause_number="4.1",
        ground_truth=GroundTruth(
            obligation_type="mandatory",
            entities=[GTEntity("Mutual Fund")],
            thresholds=[
                GTThreshold("Expense Ratio (Equity Direct)", "range", 0.10, 1.05, "%", "equity-oriented direct plan"),
                GTThreshold("Expense Ratio (Debt Direct)", "range", 0.10, 0.80, "%", "debt-oriented direct plan"),
                GTThreshold("Expense Ratio (Hybrid Direct)", "range", 0.10, 1.00, "%", "hybrid direct plan"),
            ],
        ),
    ),
    ScenarioFixture(
        scenario_id="D-007",
        category="D",
        description="Footnote-style carve-out buried after a table description.",
        clause_text=(
            "Category-wise FPI investment limits in government debt instruments: "
            "General Category — INR 6 lakh crore; Long-term Category — INR 3 lakh "
            "crore; Voluntary Retention Route — INR 1.5 lakh crore. *The above "
            "limits are exclusive of State Development Loans, which carry a separate "
            "sub-limit of INR 25,000 crore."
        ),
        circular_number="SEBI/HO/AFD/AFD-1/CIR/P/2026/37",
        clause_number="3.5",
        ground_truth=GroundTruth(
            obligation_type="mandatory",
            entities=[GTEntity("Foreign Portfolio Investor")],
            thresholds=[
                GTThreshold("FPI Limit (General Category)", "<=", 600000.0, None, "INR crore", "general category government debt"),
                GTThreshold("FPI Limit (Long-term Category)", "<=", 300000.0, None, "INR crore", "long-term category government debt"),
                GTThreshold("FPI Limit (VRR)", "<=", 150000.0, None, "INR crore", "Voluntary Retention Route"),
                GTThreshold("FPI Sub-limit (SDLs)", "<=", 25000.0, None, "INR crore", "State Development Loans"),
            ],
        ),
    ),
    ScenarioFixture(
        scenario_id="D-008",
        category="D",
        description="Rating-based concentration limit in tabular prose form.",
        clause_text=(
            "A mutual fund shall not invest more than the following percentages of "
            "its NAV in debt instruments of a single issuer based on credit rating: "
            "AAA rated — 10%; AA rated — 8%; A rated — 6%; below A — 4%."
        ),
        circular_number="SEBI/HO/IMD/DOF3/CIR/P/2026/38",
        clause_number="5.3",
        ground_truth=GroundTruth(
            obligation_type="prohibited",
            entities=[GTEntity("Mutual Fund")],
            thresholds=[
                GTThreshold("Single-Issuer Exposure (AAA)", "<=", 10.0, None, "%", "AAA-rated debt instruments NAV"),
                GTThreshold("Single-Issuer Exposure (AA)", "<=", 8.0, None, "%", "AA-rated debt instruments NAV"),
                GTThreshold("Single-Issuer Exposure (A)", "<=", 6.0, None, "%", "A-rated debt instruments NAV"),
                GTThreshold("Single-Issuer Exposure (below A)", "<=", 4.0, None, "%", "below A-rated debt instruments NAV"),
            ],
        ),
    ),
    ScenarioFixture(
        scenario_id="D-009",
        category="D",
        description="Age-based KYC re-verification schedule embedded in prose table.",
        clause_text=(
            "Intermediaries shall conduct periodic KYC reviews based on client risk "
            "category: low-risk clients — once every 10 years; medium-risk clients "
            "— once every 8 years; high-risk clients — once every 2 years."
        ),
        circular_number="SEBI/HO/MIRSD/SECFATF/CIR/P/2026/39",
        clause_number="7.3",
        ground_truth=GroundTruth(
            obligation_type="mandatory",
            entities=[GTEntity("Market Intermediary")],
            thresholds=[
                GTThreshold("KYC Review Frequency (Low Risk)", "<=", 10.0, None, "years", "low-risk clients"),
                GTThreshold("KYC Review Frequency (Medium Risk)", "<=", 8.0, None, "years", "medium-risk clients"),
                GTThreshold("KYC Review Frequency (High Risk)", "<=", 2.0, None, "years", "high-risk clients"),
            ],
            triggers=[GTTrigger("Periodic KYC Review", "risk-category dependent")],
        ),
    ),
    ScenarioFixture(
        scenario_id="D-010",
        category="D",
        description="Lot-size schedule for index options at different strike distances.",
        clause_text=(
            "The contract size for NIFTY 50 index options shall be: "
            "at-the-money strike — lot size of 50 units; "
            "strikes within 10% of ATM — lot size of 50 units; "
            "strikes more than 10% but within 20% of ATM — lot size of 100 units; "
            "strikes more than 20% of ATM — lot size of 200 units."
        ),
        circular_number="SEBI/HO/MRD/DP/CIR/P/2026/40",
        clause_number="2.6",
        ground_truth=GroundTruth(
            obligation_type="mandatory",
            entities=[GTEntity("Stock Exchange")],
            thresholds=[
                GTThreshold("NIFTY Option Lot Size (ATM / within 10%)", "==", 50.0, None, "units", "ATM to 10% from ATM"),
                GTThreshold("NIFTY Option Lot Size (10-20% from ATM)", "==", 100.0, None, "units", "10–20% from ATM"),
                GTThreshold("NIFTY Option Lot Size (>20% from ATM)", "==", 200.0, None, "units", ">20% from ATM"),
                GTThreshold("Strike Band for Expanded Lot", ">", 10.0, None, "%", "distance from ATM"),
            ],
        ),
    ),
]


# ---------------------------------------------------------------------------
# Category E — Edge cases and qualitative-only clauses
# ---------------------------------------------------------------------------

CAT_E: list[ScenarioFixture] = [
    ScenarioFixture(
        scenario_id="E-001",
        category="E",
        description="Pure qualitative obligation — no numeric content whatsoever.",
        clause_text=(
            "Every stockbroker shall establish and maintain adequate internal "
            "controls to prevent unauthorised access to client funds and securities, "
            "commensurate with the nature and scale of its operations."
        ),
        circular_number="SEBI/HO/MRD/DP/CIR/P/2026/41",
        clause_number="8.1",
        ground_truth=GroundTruth(
            obligation_type="mandatory",
            entities=[GTEntity("Stockbroker")],
            thresholds=[],  # must NOT extract any threshold
            qualitative_directives=[
                GTQualitative("adequate internal controls to prevent unauthorised access to client funds and securities"),
            ],
            expected_hitl_flags=["qualitative_directive", "no_deterministic_logic"],
        ),
    ),
    ScenarioFixture(
        scenario_id="E-002",
        category="E",
        description="Cross-circular reference — body of rule defined elsewhere.",
        clause_text=(
            "The net capital adequacy requirements for stockbrokers shall be as "
            "specified in SEBI Circular No. SEBI/HO/MRD/DP/CIR/P/2023/45 dated "
            "March 14, 2023, as amended from time to time."
        ),
        circular_number="SEBI/HO/MRD/DP/CIR/P/2026/42",
        clause_number="3.1",
        ground_truth=GroundTruth(
            obligation_type="mandatory",
            entities=[GTEntity("Stockbroker")],
            thresholds=[],  # rule body is external; must not invent numbers
            expected_ambiguous_fragments=["SEBI/HO/MRD/DP/CIR/P/2023/45"],
            expected_hitl_flags=["qualitative_directive"],
        ),
    ),
    ScenarioFixture(
        scenario_id="E-003",
        category="E",
        description="Ambiguous entity scope: clause says 'members' without specifying type.",
        clause_text=(
            "All members shall ensure that trading terminals are equipped with "
            "order throttle mechanisms capable of preventing the submission of "
            "orders at a rate exceeding the limits prescribed by the exchange."
        ),
        circular_number="SEBI/HO/MRD/DP/CIR/P/2026/43",
        clause_number="7.2",
        ground_truth=GroundTruth(
            obligation_type="mandatory",
            entities=[GTEntity("Trading Member")],  # 'members' in exchange context = trading members
            thresholds=[],
            qualitative_directives=[GTQualitative("order throttle mechanisms capable of preventing order rate exceedance")],
        ),
    ),
    ScenarioFixture(
        scenario_id="E-004",
        category="E",
        description="Recommended (non-mandatory) best practice guidance.",
        clause_text=(
            "Stock exchanges are encouraged to adopt a pre-trade risk management "
            "system that validates each order against at least five risk parameters "
            "before routing to the matching engine."
        ),
        circular_number="SEBI/HO/MRD/DP/CIR/P/2026/44",
        clause_number="6.1",
        ground_truth=GroundTruth(
            obligation_type="recommended",
            entities=[GTEntity("Stock Exchange")],
            thresholds=[GTThreshold("Pre-trade Risk Parameters", ">=", 5.0, None, "parameters", None)],
        ),
    ),
    ScenarioFixture(
        scenario_id="E-005",
        category="E",
        description="Obligation triggered by external regulatory event, not a calendar date.",
        clause_text=(
            "In the event of a declared market emergency as notified by the "
            "Ministry of Finance, all trading members shall suspend proprietary "
            "trading activities within 30 minutes of receipt of the notification."
        ),
        circular_number="SEBI/HO/MRD/DP/CIR/P/2026/45",
        clause_number="9.1",
        ground_truth=GroundTruth(
            obligation_type="conditional",
            entities=[GTEntity("Trading Member")],
            thresholds=[GTThreshold("Suspension Deadline", "<=", 30.0, None, "minutes", "after receiving notification")],
            triggers=[GTTrigger("Market Emergency Declaration", "event-driven")],
        ),
    ),
    ScenarioFixture(
        scenario_id="E-006",
        category="E",
        description="Multi-entity clause where obligation applies to both jointly.",
        clause_text=(
            "Both the lead manager and co-manager of a public issue shall be "
            "jointly and severally liable for the accuracy of the disclosures "
            "in the offer document and shall conduct independent due diligence."
        ),
        circular_number="SEBI/HO/CFD/DIL1/CIR/P/2026/46",
        clause_number="4.3",
        ground_truth=GroundTruth(
            obligation_type="mandatory",
            entities=[GTEntity("Lead Manager"), GTEntity("Co-manager")],
            thresholds=[],
            qualitative_directives=[
                GTQualitative("jointly and severally liable for accuracy of disclosures"),
                GTQualitative("independent due diligence"),
            ],
        ),
    ),
    ScenarioFixture(
        scenario_id="E-007",
        category="E",
        description="Obligation with sunset clause: automatically expires on a date.",
        clause_text=(
            "As a temporary measure applicable until March 31, 2027, mutual funds "
            "may invest up to 15% of their debt portfolio in perpetual bonds issued "
            "by public sector banks; thereafter the limit shall revert to 10%."
        ),
        circular_number="SEBI/HO/IMD/DOF3/CIR/P/2026/47",
        clause_number="5.7",
        ground_truth=GroundTruth(
            obligation_type="conditional",
            entities=[GTEntity("Mutual Fund")],
            thresholds=[
                GTThreshold("Perpetual Bond Limit (Temporary)", "<=", 15.0, None, "%", "debt portfolio — until Mar 31 2027"),
                GTThreshold("Perpetual Bond Limit (Permanent)", "<=", 10.0, None, "%", "debt portfolio — post Mar 31 2027"),
            ],
        ),
    ),
    ScenarioFixture(
        scenario_id="E-008",
        category="E",
        description="Negative obligation stated as 'shall refrain from'.",
        clause_text=(
            "Research analysts shall refrain from participating in any investment "
            "banking activity, roadshow, or deal solicitation relating to an issuer "
            "whose securities they cover in their research reports."
        ),
        circular_number="SEBI/HO/DAFN/DAFN-RA/P/CIR/2026/48",
        clause_number="3.2",
        ground_truth=GroundTruth(
            obligation_type="prohibited",
            entities=[GTEntity("Research Analyst")],
            thresholds=[],
            qualitative_directives=[
                GTQualitative("shall refrain from investment banking activity, roadshow, or deal solicitation for covered issuers"),
            ],
        ),
    ),
    ScenarioFixture(
        scenario_id="E-009",
        category="E",
        description="Obligation stated via passive voice with implicit entity.",
        clause_text=(
            "Prior approval must be obtained from SEBI before any material change "
            "is made to the fundamental attributes of a mutual fund scheme, including "
            "any change in the investment objective, asset allocation, or benchmark index."
        ),
        circular_number="SEBI/HO/IMD/DOF3/CIR/P/2026/49",
        clause_number="6.3",
        ground_truth=GroundTruth(
            obligation_type="mandatory",
            entities=[GTEntity("Mutual Fund"), GTEntity("Asset Management Company")],
            thresholds=[],
            qualitative_directives=[
                GTQualitative("prior SEBI approval required for material change to fundamental attributes"),
            ],
        ),
    ),
    ScenarioFixture(
        scenario_id="E-050",
        category="E",
        description="Clause entirely composed of definitions — no extractable obligation.",
        clause_text=(
            "For the purpose of this circular: (a) 'client' means any person "
            "on whose behalf a stockbroker executes trades; (b) 'proprietary "
            "trading' means trading on the stockbroker's own account using its "
            "own capital; (c) 'margin shortfall' means the difference between "
            "the required margin and the margin actually collected or available."
        ),
        circular_number="SEBI/HO/MRD/DP/CIR/P/2026/50",
        clause_number="1.1",
        ground_truth=GroundTruth(
            obligation_type="recommended",   # no obligation — definitions only
            entities=[],
            thresholds=[],
            qualitative_directives=[],
            expected_ambiguous_fragments=["'client'", "'proprietary trading'", "'margin shortfall'"],
            expected_hitl_flags=["qualitative_directive", "no_deterministic_logic"],
        ),
    ),
]


# ---------------------------------------------------------------------------
# Master fixture list
# ---------------------------------------------------------------------------

ALL_SCENARIOS: list[ScenarioFixture] = CAT_A + CAT_B + CAT_C + CAT_D + CAT_E

assert len(ALL_SCENARIOS) == 50, f"Expected 50 scenarios, got {len(ALL_SCENARIOS)}"


def get_scenarios_by_category(category: str) -> list[ScenarioFixture]:
    return [s for s in ALL_SCENARIOS if s.category == category]


def get_scenario_by_id(scenario_id: str) -> ScenarioFixture:
    for s in ALL_SCENARIOS:
        if s.scenario_id == scenario_id:
            return s
    raise KeyError(f"No scenario with id={scenario_id!r}")
