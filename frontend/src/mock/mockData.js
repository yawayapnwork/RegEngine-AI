// Mock state shaped to mirror the real backend contracts so swapping fetch()
// calls in later (see api.js) requires no component changes:
//   pipeline runs   <- app.parsing / app.agents (ingestion -> extraction -> audit -> compile)
//   clauses         <- app.compiler.models.CompiledRego + app.agents.schemas.ExtractedComplianceRule
//   hitlCases       <- app.execution.models.HITLCase / app.compiler.models.HITLFlag
//   ledgerFeed      <- app.ledger.models.LedgerEntry (hash-chained audit vault)

export const PIPELINE_STAGES = [
  "ingestion",
  "extraction",
  "verification",
  "compilation",
];

export const pipelineRuns = [
  {
    id: "run-2026-0142",
    filename: "SEBI_Master_Circular_MIRSD_2026.pdf",
    circularNumber: "SEBI/HO/MIRSD/2026/01",
    startedAt: "2026-08-28T09:12:04Z",
    currentStage: "compilation",
    stages: {
      ingestion: {
        status: "complete",
        detail: "312 pages parsed, 486 layout elements extracted",
        durationMs: 8210,
      },
      extraction: {
        status: "complete",
        detail: "CrewAI dual-agent pass: 94 clauses -> 61 extracted rules",
        durationMs: 142300,
      },
      verification: {
        status: "complete",
        detail: "Logic Auditor Agent: 54 approved, 7 flagged for HITL",
        durationMs: 51900,
      },
      compilation: {
        status: "in_progress",
        detail: "Compiling Rego + JSON-Logic for 54 approved rules (38/54)",
        durationMs: null,
      },
    },
  },
  {
    id: "run-2026-0141",
    filename: "SEBI_Circular_Margin_Trading_Amendment.pdf",
    circularNumber: "SEBI/HO/MIRSD/2026/07",
    startedAt: "2026-08-27T14:03:11Z",
    currentStage: "done",
    stages: {
      ingestion: {
        status: "complete",
        detail: "41 pages parsed, 88 layout elements extracted",
        durationMs: 3100,
      },
      extraction: {
        status: "complete",
        detail: "12 clauses -> 9 extracted rules",
        durationMs: 38200,
      },
      verification: {
        status: "complete",
        detail: "8 approved, 1 flagged (qualitative directive)",
        durationMs: 11400,
      },
      compilation: {
        status: "complete",
        detail: "8 Rego modules published to OPA",
        durationMs: 2600,
      },
    },
  },
  {
    id: "run-2026-0143",
    filename: "SEBI_Circular_AlgoTrading_RiskControls.pdf",
    circularNumber: "SEBI/HO/MIRSD/2026/11",
    startedAt: "2026-08-28T11:47:52Z",
    currentStage: "extraction",
    stages: {
      ingestion: {
        status: "complete",
        detail: "76 pages parsed, 133 layout elements extracted",
        durationMs: 5400,
      },
      extraction: {
        status: "in_progress",
        detail: "Extraction Agent processing chunk 19/44",
        durationMs: null,
      },
      verification: {
        status: "pending",
        detail: "Waiting on extraction",
        durationMs: null,
      },
      compilation: {
        status: "pending",
        detail: "Waiting on verification",
        durationMs: null,
      },
    },
  },
];

export const clauses = [
  {
    ruleId: "9f2a6c1e4b7d0853f1a9c6e2b4d7f108:3.2.1",
    circularNumber: "SEBI/HO/MIRSD/2026/01",
    clauseNumber: "3.2.1",
    sourceSha256: "9f2a6c1e4b7d0853f1a9c6e2b4d7f108",
    title: "Upfront Margin Requirement",
    rawText:
      "Every stockbroker shall collect an upfront margin of not less than [[20% (twenty percent)]] of the transaction value from the client before the execution of any trade in the derivatives segment. [[Where the margin collected falls short of this threshold]], the stockbroker shall report the shortfall to the Clearing Corporation within one trading day and shall not permit further leveraged positions for that client until the shortfall is cured.",
    highlights: [
      { text: "20% (twenty percent)", kind: "threshold", regoAnchor: ">= 20" },
      {
        text: "Where the margin collected falls short of this threshold",
        kind: "condition",
        regoAnchor: "violation contains msg",
      },
    ],
    regoCode: `package sebi.circulars.sebi_ho_mirsd_2026_01.clause_3_2_1

import rego.v1

default allow := false

entity_matches if { input.entity_type == "Stockbroker" }

# Upfront Margin >= 20%
cond_0 if {
    input.facts.upfront_margin_pct >= 20
}

allow if {
    entity_matches
    cond_0
}

violation contains msg if {
    entity_matches
    input.facts.upfront_margin_pct < 20
    msg := sprintf("%s is %v %s, which fails the required condition (>= %v %s, clause 3.2.1)",
        ["Upfront Margin", input.facts.upfront_margin_pct, "%", "20", "%"])
}

deny := violation

decision := {
    "allow": allow,
    "violations": violation,
    "rule_id": "9f2a6c1e4b7d0853f1a9c6e2b4d7f108:3.2.1",
    "clause_number": "3.2.1",
    "circular_number": "SEBI/HO/MIRSD/2026/01",
    "obligation_type": "mandatory",
}`,
    jsonLogic: {
      and: [
        { "==": [{ var: "entity_type" }, "Stockbroker"] },
        { ">=": [{ var: "facts.upfront_margin_pct" }, 20] },
      ],
    },
    sampleTransaction: {
      transaction_id: "PLAYGROUND-TXN-0001",
      entity_type: "Stockbroker",
      facts: { upfront_margin_pct: 15 },
    },
    status: "compiled",
  },
  {
    ruleId: "a15e88d2c0f3417ab6dfe9c04a2b7311:4.1.0",
    circularNumber: "SEBI/HO/MIRSD/2026/01",
    clauseNumber: "4.1.0",
    sourceSha256: "a15e88d2c0f3417ab6dfe9c04a2b7311",
    title: "Adequate Internal Controls (Qualitative)",
    rawText:
      "Every stockbroker shall maintain [[adequate internal controls and risk management systems]] commensurate with the scale and nature of its business, and shall periodically review the effectiveness of such systems through an independent audit.",
    highlights: [
      {
        text: "adequate internal controls and risk management systems",
        kind: "qualitative",
        regoAnchor: null,
      },
    ],
    regoCode: null,
    jsonLogic: null,
    sampleTransaction: { transaction_id: "PLAYGROUND-TXN-0002", entity_type: "Stockbroker", facts: {} },
    status: "hitl_blocked",
  },
  {
    ruleId: "5c7d21f0e8b4913ac0d6f2b8a7c4e905:2.4.3",
    circularNumber: "SEBI/HO/MIRSD/2026/01",
    clauseNumber: "2.4.3",
    sourceSha256: "5c7d21f0e8b4913ac0d6f2b8a7c4e905",
    title: "Net Worth Threshold",
    rawText:
      "No entity shall be granted registration as a stockbroker unless it maintains a minimum net worth of [[INR 5 (five) crore]] at all times during the currency of registration.",
    highlights: [
      { text: "INR 5 (five) crore", kind: "threshold", regoAnchor: ">= 5" },
    ],
    regoCode: `package sebi.circulars.sebi_ho_mirsd_2026_01.clause_2_4_3

import rego.v1

default allow := false

entity_matches if { input.entity_type == "Stockbroker" }

# Net Worth >= 5 INR Crore
cond_0 if {
    input.facts.net_worth_inr_crore >= 5
}

allow if {
    entity_matches
    cond_0
}

violation contains msg if {
    entity_matches
    input.facts.net_worth_inr_crore < 5
    msg := sprintf("%s is %v %s, which fails the required condition (>= %v %s, clause 2.4.3)",
        ["Net Worth", input.facts.net_worth_inr_crore, "inr_crore", "5", "inr_crore"])
}

deny := violation

decision := {
    "allow": allow,
    "violations": violation,
    "rule_id": "5c7d21f0e8b4913ac0d6f2b8a7c4e905:2.4.3",
    "clause_number": "2.4.3",
    "circular_number": "SEBI/HO/MIRSD/2026/01",
    "obligation_type": "mandatory",
}`,
    jsonLogic: {
      and: [
        { "==": [{ var: "entity_type" }, "Stockbroker"] },
        { ">=": [{ var: "facts.net_worth_inr_crore" }, 5] },
      ],
    },
    sampleTransaction: {
      transaction_id: "PLAYGROUND-TXN-0003",
      entity_type: "Stockbroker",
      facts: { net_worth_inr_crore: 3 },
    },
    status: "compiled",
  },
];

export const hitlCases = [
  {
    caseId: "hitl-8841",
    kind: "compiler", // a rule the compiler couldn't safely compile
    ruleId: "a15e88d2c0f3417ab6dfe9c04a2b7311:4.1.0",
    clauseNumber: "4.1.0",
    circularNumber: "SEBI/HO/MIRSD/2026/01",
    reasonCode: "qualitative_directive",
    severity: "advisory",
    description:
      'Qualitative directive cannot be programmatically enforced: "adequate internal controls and risk management systems". Requires a human-authored control (policy attestation, manual checklist, or narrative audit procedure).',
    sourceExcerpt: "adequate internal controls and risk management systems",
    flaggedAt: "2026-08-28T10:41:09Z",
    status: "pending",
  },
  {
    caseId: "hitl-8842",
    kind: "compiler",
    ruleId: "c920f4e1b6a8d735fc10e29b5a3d7f66:5.6.2",
    clauseNumber: "5.6.2",
    circularNumber: "SEBI/HO/MIRSD/2026/01",
    reasonCode: "conflicting_thresholds",
    severity: "blocking",
    description:
      "Conflicting thresholds extracted for 'margin_shortfall_pct|%': lower bound(s) [25] exceed upper bound(s) [15]. This combination can never be satisfied and likely indicates an extraction error.",
    sourceExcerpt:
      "the shortfall percentage shall be not less than 25% and not more than 15%",
    flaggedAt: "2026-08-28T10:41:12Z",
    status: "pending",
  },
  {
    caseId: "hitl-7710",
    kind: "execution", // an ambiguous live transaction the OPA engine couldn't decide
    transactionId: "TXN-88213409",
    brokerId: "BRK-00294",
    ruleId: "9f2a6c1e4b7d0853f1a9c6e2b4d7f108:3.2.1",
    reason:
      "Policy(ies) ['9f2a6c1e4b7d0853f1a9c6e2b4d7f108:3.2.1'] returned an undefined result — insufficient or malformed facts.",
    facts: { entity_type: "Stockbroker", facts: { upfront_margin_pct: null } },
    flaggedAt: "2026-08-28T12:03:55Z",
    status: "pending",
  },
  {
    caseId: "hitl-7699",
    kind: "execution",
    transactionId: "TXN-88198871",
    brokerId: "BRK-00107",
    ruleId: "5c7d21f0e8b4913ac0d6f2b8a7c4e905:2.4.3",
    reason:
      "Policy(ies) ['5c7d21f0e8b4913ac0d6f2b8a7c4e905:2.4.3'] returned an undefined result — insufficient or malformed facts.",
    facts: {
      entity_type: "Stockbroker",
      facts: { net_worth_inr_crore: undefined },
    },
    flaggedAt: "2026-08-28T08:55:02Z",
    status: "approved",
    resolvedBy: "priya.sharma@compliance",
    resolvedAt: "2026-08-28T09:10:44Z",
    notes: "Confirmed via manual net-worth certificate on file. Cleared.",
  },
];

export const ledgerFeed = [
  {
    sequenceNum: 40231,
    transactionId: "TXN-88221190",
    brokerId: "BRK-00294",
    evaluatedAt: "2026-08-28T12:14:02Z",
    circularId: "SEBI/HO/MIRSD/2026/01",
    clauseHash: "9f2a6c1e4b7d0853f1a9c6e2b4d7f108",
    sectionReference: "3.2.1",
    ruleId: "9f2a6c1e4b7d0853f1a9c6e2b4d7f108:3.2.1",
    evaluationResult: "PASS",
    previousHash: "6a41f0c8e2b7...d914",
    currentHash: "e0b3d7a9c1f4...2a67",
  },
  {
    sequenceNum: 40230,
    transactionId: "TXN-88221187",
    brokerId: "BRK-00512",
    evaluatedAt: "2026-08-28T12:13:41Z",
    circularId: "SEBI/HO/MIRSD/2026/01",
    clauseHash: "5c7d21f0e8b4913ac0d6f2b8a7c4e905",
    sectionReference: "2.4.3",
    ruleId: "5c7d21f0e8b4913ac0d6f2b8a7c4e905:2.4.3",
    evaluationResult: "FAIL",
    previousHash: "b7c92e1f4a08...5cd3",
    currentHash: "6a41f0c8e2b7...d914",
  },
  {
    sequenceNum: 40229,
    transactionId: "TXN-88221183",
    brokerId: "BRK-00107",
    evaluatedAt: "2026-08-28T12:12:58Z",
    circularId: "SEBI/HO/MIRSD/2026/01",
    clauseHash: "9f2a6c1e4b7d0853f1a9c6e2b4d7f108",
    sectionReference: "3.2.1",
    ruleId: "9f2a6c1e4b7d0853f1a9c6e2b4d7f108:3.2.1",
    evaluationResult: "HITL_REVIEW",
    hitlReviewId: "hitl-7710",
    previousHash: "3f18a6d0c9e2...771b",
    currentHash: "b7c92e1f4a08...5cd3",
  },
  {
    sequenceNum: 40228,
    transactionId: "TXN-88221179",
    brokerId: "BRK-00294",
    evaluatedAt: "2026-08-28T12:12:10Z",
    circularId: "SEBI/HO/MIRSD/2026/01",
    clauseHash: "9f2a6c1e4b7d0853f1a9c6e2b4d7f108",
    sectionReference: "3.2.1",
    ruleId: "9f2a6c1e4b7d0853f1a9c6e2b4d7f108:3.2.1",
    evaluationResult: "PASS",
    previousHash: "0d5b2e8c1a4f...93e0",
    currentHash: "3f18a6d0c9e2...771b",
  },
];
