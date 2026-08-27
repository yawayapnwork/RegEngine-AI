"""Guardrail tools shared by the Extraction and Logic Auditor agents.

Each tool is implemented first as a plain, synchronous, dependency-light
Python function with its own Pydantic input/output schema — these are unit
testable without CrewAI or an LLM. `crewai.tools.BaseTool` subclasses wrap
them at the bottom; the `crewai` import is deferred into that section so the
core logic (and its tests) never require the package to be installed.

These tools exist specifically to make the "no hallucination" requirement
mechanically enforceable rather than a matter of prompt phrasing:
  - QuoteVerificationTool: is a `verbatim_evidence` string actually a
    substring of the source clause? Catches invented quotes outright.
  - NumericPatternScannerTool: what numeric/percentage/currency tokens
    actually exist in the source? Lets the auditor diff extracted
    thresholds against ground truth instead of trusting the extractor.
  - EntityTaxonomyLookupTool: normalizes/validates entity phrases against
    a controlled SEBI entity vocabulary, surfacing low-confidence or
    unresolvable entity claims for auditor scrutiny.
  - ClauseContextTool: surfaces sibling/parent clauses so both agents can
    check for cross-references (e.g. "as specified in 2.1") that live
    outside the current chunk, addressing "missing context" findings.
"""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------
# Controlled SEBI entity taxonomy (extend as needed)
# --------------------------------------------------------------------------

SEBI_ENTITY_TAXONOMY: dict[str, list[str]] = {
    "Stockbroker": ["stock broker", "stockbroker", "trading member", "broker"],
    "Depository Participant": ["depository participant", "dp"],
    "Asset Management Company": ["amc", "asset management company", "mutual fund manager"],
    "Mutual Fund Trustee": ["trustee", "mutual fund trustee"],
    "Custodian": ["custodian"],
    "Clearing Corporation": ["clearing corporation", "clearing house", "ccl"],
    "Stock Exchange": ["stock exchange", "recognized stock exchange", "exchange"],
    "Merchant Banker": ["merchant banker", "lead manager"],
    "Credit Rating Agency": ["credit rating agency", "cra"],
    "Investment Adviser": ["investment adviser", "ia"],
    "Portfolio Manager": ["portfolio manager", "pms"],
    "Registrar and Transfer Agent": ["registrar and transfer agent", "rta"],
    "KYC Registration Agency": ["kyc registration agency", "kra"],
    "Research Analyst": ["research analyst"],
    "Alternative Investment Fund": ["alternative investment fund", "aif"],
    "Foreign Portfolio Investor": ["foreign portfolio investor", "fpi"],
}


def _canon(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).lower().split())


# --------------------------------------------------------------------------
# QuoteVerificationTool
# --------------------------------------------------------------------------


class QuoteCheckInput(BaseModel):
    quotes: list[str] = Field(..., description="verbatim_evidence strings to check against the source.")
    source_text: str = Field(..., description="Raw source clause text the extraction was derived from.")
    fuzzy_threshold: float = Field(0.92, description="Similarity ratio below which a near-miss is treated as unverified.")


class QuoteCheckResult(BaseModel):
    quote: str
    exact_match: bool
    best_similarity: float
    verified: bool


class QuoteCheckOutput(BaseModel):
    results: list[QuoteCheckResult]
    verified_count: int
    unverified_count: int


def verify_quotes(input: QuoteCheckInput) -> QuoteCheckOutput:
    source_canon = _canon(input.source_text)
    results: list[QuoteCheckResult] = []

    for quote in input.quotes:
        q_canon = _canon(quote)
        if not q_canon:
            results.append(QuoteCheckResult(quote=quote, exact_match=False, best_similarity=0.0, verified=False))
            continue

        exact = q_canon in source_canon
        if exact:
            results.append(QuoteCheckResult(quote=quote, exact_match=True, best_similarity=1.0, verified=True))
            continue

        # Sliding window fuzzy match to tolerate minor OCR/whitespace drift
        # without accepting genuinely fabricated quotes.
        window = len(q_canon)
        best = 0.0
        step = max(1, window // 4)
        for i in range(0, max(1, len(source_canon) - window + 1), step):
            segment = source_canon[i : i + window]
            ratio = SequenceMatcher(None, q_canon, segment).ratio()
            best = max(best, ratio)
            if best >= 0.999:
                break

        results.append(
            QuoteCheckResult(
                quote=quote,
                exact_match=False,
                best_similarity=round(best, 4),
                verified=best >= input.fuzzy_threshold,
            )
        )

    verified = sum(1 for r in results if r.verified)
    return QuoteCheckOutput(results=results, verified_count=verified, unverified_count=len(results) - verified)


# --------------------------------------------------------------------------
# NumericPatternScannerTool
# --------------------------------------------------------------------------

_NUMERIC_PATTERN = re.compile(
    r"""
    (?P<value>\d[\d,]*(?:\.\d+)?)          # 20, 1,000, 20.5
    \s*
    (?P<unit>%|percent|per\ cent|crore|lakh|bps|basis\ points|days?|months?|years?|hours?|INR|Rs\.?|₹)?
    """,
    re.IGNORECASE | re.VERBOSE,
)


class NumericScanInput(BaseModel):
    source_text: str
    context_window: int = Field(40, description="Characters of surrounding context to capture on each side.")


class NumericToken(BaseModel):
    raw_match: str
    value: float
    unit: str | None
    context: str


class NumericScanOutput(BaseModel):
    tokens: list[NumericToken]


def scan_numeric_tokens(input: NumericScanInput) -> NumericScanOutput:
    tokens: list[NumericToken] = []
    text = input.source_text
    for m in _NUMERIC_PATTERN.finditer(text):
        raw_value = m.group("value")
        if not raw_value:
            continue
        try:
            value = float(raw_value.replace(",", ""))
        except ValueError:
            continue
        start = max(0, m.start() - input.context_window)
        end = min(len(text), m.end() + input.context_window)
        tokens.append(
            NumericToken(
                raw_match=m.group(0).strip(),
                value=value,
                unit=(m.group("unit") or "").strip() or None,
                context=text[start:end].strip(),
            )
        )
    return NumericScanOutput(tokens=tokens)


# --------------------------------------------------------------------------
# EntityTaxonomyLookupTool
# --------------------------------------------------------------------------


class EntityLookupInput(BaseModel):
    entity_phrase: str


class EntityLookupOutput(BaseModel):
    input_phrase: str
    normalized_entity: str | None
    confidence: float
    resolved: bool


def lookup_entity(input: EntityLookupInput) -> EntityLookupOutput:
    phrase_canon = _canon(input.entity_phrase)
    best_entity: str | None = None
    best_score = 0.0

    for canonical, aliases in SEBI_ENTITY_TAXONOMY.items():
        for alias in aliases + [canonical]:
            alias_canon = _canon(alias)
            if alias_canon == phrase_canon:
                return EntityLookupOutput(input_phrase=input.entity_phrase, normalized_entity=canonical, confidence=1.0, resolved=True)
            if alias_canon in phrase_canon or phrase_canon in alias_canon:
                score = len(alias_canon) / max(len(phrase_canon), 1)
                if score > best_score:
                    best_score, best_entity = score, canonical

    resolved = best_entity is not None and best_score >= 0.5
    return EntityLookupOutput(
        input_phrase=input.entity_phrase,
        normalized_entity=best_entity if resolved else None,
        confidence=round(best_score, 4),
        resolved=resolved,
    )


# --------------------------------------------------------------------------
# ClauseContextTool
# --------------------------------------------------------------------------


class ClauseContextInput(BaseModel):
    clause_number: str | None
    section_path: list[str]
    all_chunks: list[dict] = Field(
        ..., description="Sibling ClauseChunk dicts from the same circular (chunk_id, clause_number, section_path, text)."
    )


class ClauseContextOutput(BaseModel):
    parent_section_text: list[str]
    sibling_clause_numbers: list[str]
    cross_reference_hits: list[str] = Field(
        default_factory=list, description="Other clause numbers explicitly mentioned inline in this clause's own text."
    )


_CROSS_REF_PATTERN = re.compile(r"\b(?:clause|paragraph|para|section)?\s*(\d+(?:\.\d+){0,3}[a-z]?)\b", re.IGNORECASE)


def build_clause_context(input: ClauseContextInput, current_text: str) -> ClauseContextOutput:
    parent_path = input.section_path[:-1] if input.section_path else []
    parent_texts = [
        c["text"]
        for c in input.all_chunks
        if c.get("clause_number") and c["clause_number"] in parent_path
    ]
    siblings = [
        c["clause_number"]
        for c in input.all_chunks
        if c.get("section_path") == input.section_path and c.get("clause_number") != input.clause_number
    ]
    referenced = sorted(
        {
            m.group(1)
            for m in _CROSS_REF_PATTERN.finditer(current_text)
            if m.group(1) != (input.clause_number or "")
        }
    )
    return ClauseContextOutput(parent_section_text=parent_texts, sibling_clause_numbers=siblings, cross_reference_hits=referenced)


# --------------------------------------------------------------------------
# CrewAI tool wrappers (deferred import)
# --------------------------------------------------------------------------


def build_crewai_tools() -> list:
    """Instantiate CrewAI BaseTool wrappers around the functions above.
    Imports crewai lazily so this module (and the pure functions) stay
    importable/testable without the crewai package installed."""
    from crewai.tools import tool  # deferred heavy import

    @tool("verify_quotes")
    def verify_quotes_tool(quotes: list[str], source_text: str) -> dict:
        """Check whether each verbatim_evidence quote is an exact (or near-exact)
        substring of the raw source clause text. Returns per-quote verification
        results. ALWAYS call this before approving any field that carries a
        verbatim_evidence quote."""
        return verify_quotes(QuoteCheckInput(quotes=quotes, source_text=source_text)).model_dump()

    @tool("scan_numeric_tokens")
    def scan_numeric_tokens_tool(source_text: str) -> dict:
        """Scan the raw source text for every numeric/percentage/currency token
        actually present, with surrounding context. Use this to check whether an
        extracted numerical threshold has a real basis in the source, or was
        fabricated."""
        return scan_numeric_tokens(NumericScanInput(source_text=source_text)).model_dump()

    @tool("lookup_entity")
    def lookup_entity_tool(entity_phrase: str) -> dict:
        """Normalize a free-text entity phrase against the controlled SEBI entity
        taxonomy (Stockbroker, AMC, Custodian, ...). Returns the canonical entity
        name and a confidence score; resolved=false means the phrase could not be
        matched to a known entity type and should be treated cautiously."""
        return lookup_entity(EntityLookupInput(entity_phrase=entity_phrase)).model_dump()

    @tool("build_clause_context")
    def build_clause_context_tool(clause_number: str | None, section_path: list[str], all_chunks: list[dict], current_text: str) -> dict:
        """Fetch parent-section text, sibling clause numbers, and inline
        cross-references (e.g. 'as specified in 2.1') for the current clause, so
        missing-context findings can be checked against the actual document
        structure rather than guessed."""
        return build_clause_context(
            ClauseContextInput(clause_number=clause_number, section_path=section_path, all_chunks=all_chunks),
            current_text=current_text,
        ).model_dump()

    return [verify_quotes_tool, scan_numeric_tokens_tool, lookup_entity_tool, build_clause_context_tool]
