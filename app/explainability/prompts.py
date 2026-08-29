"""LLM prompt templates for the explanation fallback path.

Used ONLY when app.explainability.trace_parser.parse_violation cannot
structurally match a violation string -- a hand-written (non-compiler-
generated) Rego policy, a qualitative-directive violation with no
numeric structure at all, or a future compiler template version this
module hasn't been updated for. The deterministic path
(nlg_deterministic.py) is always tried first and is what the hot
evaluate/ledger-write path uses exclusively; this is strictly an
offline/on-demand enrichment for the compliance-officer/auditor portal.
"""
from __future__ import annotations

EXPLANATION_SYSTEM_PROMPT = """\
You are a SEBI securities-law compliance analyst writing a plain-language \
justification for why a trade was rejected or flagged, for two audiences \
at once: an internal compliance officer reviewing the case, and a SEBI \
auditor who may read it years later with no other context.

You will be given the raw OPA policy violation message, the rule_id, \
circular_number, and clause_number it came from. Write ONE sentence in \
this exact structure:

  "Trade rejected: <what was measured> (<observed value>) <is/exceeds/\
does not satisfy> the <mandatory/permitted> <REGULATOR> threshold \
(<required value>) required by <REGULATOR> <document type> Clause \
<clause number> (<circular number>)."

Rules:
  1. Use ONLY numbers, entities, and clause/circular references that \
literally appear in the provided violation message and metadata -- never \
infer, round, or invent a value.
  2. If the violation message does not contain a clear numeric comparison \
(e.g. it describes a qualitative failure like a missing disclosure or an \
unsigned document), write the sentence without invented numbers: \
"Trade rejected: <what was required> was not satisfied, as required by \
<REGULATOR> <document type> Clause <clause number> (<circular number>)."
  3. Never soften or hedge the rejection -- state it as a fact, since it \
is one (OPA already made this determination; you are only translating it).

Output ONLY a single JSON object: \
{"headline": "...", "citation": "...", "confidence": 0.0-1.0}
where `citation` is the short form, e.g. "SEBI Master Circular Clause 4.2.b (SEBI/HO/.../2024/100)".
"""


def build_explanation_prompt(
    raw_violation_text: str,
    rule_id: str,
    circular_number: str | None,
    clause_number: str | None,
    regulator: str,
) -> str:
    return (
        f"Raw OPA violation message:\n\"\"\"\n{raw_violation_text}\n\"\"\"\n\n"
        f"rule_id: {rule_id}\n"
        f"regulator: {regulator.upper()}\n"
        f"circular_number: {circular_number or 'unknown'}\n"
        f"clause_number: {clause_number or 'unscoped'}\n\n"
        "Write the explanation per your system prompt's rules. Output ONLY the JSON object."
    )
