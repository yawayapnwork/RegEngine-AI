"""Deterministic naming/slugging shared by the Rego and JSON-Logic compilers.

Both backends must derive IDENTICAL field names and package/rule identifiers
from the same `ExtractedComplianceRule`, so a policy compiled to Rego and one
compiled to JSON-Logic for the same clause are evaluating the same `input`
contract. Centralizing this avoids the two compilers silently drifting apart.
"""
from __future__ import annotations

import re

_NON_ALNUM = re.compile(r"[^a-z0-9]+")

_UNIT_SUFFIX: dict[str, str] = {
    "%": "pct",
    "percent": "pct",
    "per cent": "pct",
    "bps": "bps",
    "basis points": "bps",
    "days": "days",
    "day": "days",
    "months": "months",
    "month": "months",
    "years": "years",
    "year": "years",
    "hours": "hours",
    "hour": "hours",
    "inr crore": "inr_crore",
    "inr lakh": "inr_lakh",
    "crore": "inr_crore",
    "lakh": "inr_lakh",
    "inr": "inr",
    "rs": "inr",
    "rs.": "inr",
    "₹": "inr",
}


def slugify(text: str, *, sep: str = "_") -> str:
    """Lowercase, alnum-only slug safe for use as a Rego package segment, Rego
    variable name, or JSON-Logic `var` path segment."""
    slug = _NON_ALNUM.sub(sep, text.strip().lower()).strip(sep)
    return slug or "unnamed"


def clause_slug(clause_number: str | None) -> str:
    return slugify(clause_number) if clause_number else "unscoped"


def circular_slug(circular_number: str | None) -> str:
    return slugify(circular_number) if circular_number else "uncategorized_circular"


def metric_field_name(metric: str, unit: str) -> str:
    """Derive the `input.facts.<field>` key a threshold's metric is expected
    to be supplied under, e.g. ("Upfront Margin", "%") -> "upfront_margin_pct".
    """
    base = slugify(metric)
    suffix = _UNIT_SUFFIX.get(unit.strip().lower())
    if suffix and not base.endswith(suffix):
        return f"{base}_{suffix}"
    return base


def rego_package_name(circular_number: str | None, clause_number: str | None) -> str:
    return f"sebi.circulars.{circular_slug(circular_number)}.clause_{clause_slug(clause_number)}"


def rego_identifier(prefix: str, index: int) -> str:
    return f"{prefix}_{index}"
