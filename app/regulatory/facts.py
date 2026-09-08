"""Canonical regulatory fact taxonomy for RegEngine AI.

Enforces a controlled vocabulary of executable compliance fact identifiers.
Arbitrary LLM-extracted metric names (e.g. 'Minimum Upfront Margin', 'up-front margin')
must be deterministically mapped to canonical identifiers (e.g. 'upfront_margin_pct')
rather than dynamically generating arbitrary field names.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class FactDataType(str, Enum):
    PERCENTAGE = "percentage"
    CURRENCY = "currency"
    NUMBER = "number"
    DURATION_DAYS = "duration_days"
    DURATION_HOURS = "duration_hours"
    BOOLEAN = "boolean"
    RATIO = "ratio"


class FactValidationStatus(str, Enum):
    VALID = "valid"
    MISSING_MAPPING = "missing_mapping"
    WRONG_UNIT = "wrong_unit"
    WRONG_THRESHOLD = "wrong_threshold"


@dataclass(frozen=True)
class CanonicalFact:
    identifier: str
    display_name: str
    description: str
    data_type: FactDataType
    allowed_units: frozenset[str]
    synonyms: tuple[str, ...]
    min_value: float | None = None
    max_value: float | None = None
    regex_patterns: tuple[re.Pattern[str], ...] = ()


_NON_ALNUM_SPACE = re.compile(r"[^a-z0-9\s]+")
_MULTI_SPACE = re.compile(r"\s+")


def normalize_metric_text(text: str) -> str:
    """Lowercase, strip, replace hyphens/underscores/slashes with space,
    strip non-alphanumeric characters, and collapse multiple whitespace.
    """
    if not text:
        return ""
    cleaned = text.strip().lower()
    cleaned = cleaned.replace("-", " ").replace("_", " ").replace("/", " ")
    cleaned = _NON_ALNUM_SPACE.sub(" ", cleaned)
    return _MULTI_SPACE.sub(" ", cleaned).strip()


def normalize_unit(unit: str) -> str:
    """Normalize common unit variants to a canonical lookup key."""
    if not unit:
        return ""
    u = unit.strip().lower()
    if u in ("%", "pct", "percent", "per cent", "percentage"):
        return "%"
    if u in ("day", "days"):
        return "days"
    if u in ("hour", "hours", "hr", "hrs"):
        return "hours"
    if u in ("month", "months"):
        return "months"
    if u in ("week", "weeks"):
        return "weeks"
    if u in ("inr", "rs", "rs.", "₹", "rupees"):
        return "inr"
    if u in ("inr crore", "crore", "crores", "cr"):
        return "inr crore"
    if u in ("inr lakh", "lakh", "lakhs"):
        return "inr lakh"
    return u


# ---------------------------------------------------------------------------
# Canonical Fact Definitions
# ---------------------------------------------------------------------------

_CANONICAL_FACTS: dict[str, CanonicalFact] = {
    "upfront_margin_pct": CanonicalFact(
        identifier="upfront_margin_pct",
        display_name="Upfront Margin Percentage",
        description="Minimum upfront margin percentage required to be collected from clients prior to order execution.",
        data_type=FactDataType.PERCENTAGE,
        allowed_units=frozenset({"%", "pct", "percent", "per cent", "percentage"}),
        min_value=0.0,
        max_value=100.0,
        synonyms=(
            "upfront margin",
            "minimum upfront margin",
            "upfront margin percentage",
            "up front margin",
            "initial margin",
            "minimum initial margin",
            "upfront margin requirement",
            "margin upfront",
            "mandatory upfront margin",
            "minimum upfront margin required",
            "upfront margin rate",
            "margin",
            "minimum margin",
            "upfront margin collection",
            "client upfront margin",
        ),
        regex_patterns=(
            re.compile(r"^(?:minimum\s+|mandatory\s+|client\s+)?up\s*front\s+margin(?:\s+percentage|\s+pct|\s+requirement|\s+rate|\s+collection|\s+required)?$"),
            re.compile(r"^(?:minimum\s+|mandatory\s+)?initial\s+margin(?:\s+requirement|\s+percentage)?$"),
        ),
    ),
    "client_collateral": CanonicalFact(
        identifier="client_collateral",
        display_name="Client Collateral",
        description="Total eligible collateral or minimum collateral value deposited by a client.",
        data_type=FactDataType.CURRENCY,
        allowed_units=frozenset({"inr", "inr crore", "inr lakh", "crore", "lakh", "rs", "rs.", "₹", "rupees", "%", "pct"}),
        min_value=0.0,
        max_value=None,
        synonyms=(
            "client collateral",
            "minimum client collateral",
            "client collateral collection",
            "client margin collateral",
            "collateral from clients",
            "client collateral requirement",
            "collateral of client",
            "client collateral amount",
            "minimum collateral",
            "client collateral balance",
            "collateral requirement",
            "client collateral deposit",
        ),
        regex_patterns=(
            re.compile(r"^(?:minimum\s+|mandatory\s+)?(?:client\s+)?collateral(?:\s+from\s+clients|\s+collection|\s+balance|\s+amount|\s+deposit)?(?:\s+requirement)?$"),
            re.compile(r"^(?:minimum\s+|mandatory\s+)?collateral\s+of\s+client$"),
        ),
    ),
    "peak_margin": CanonicalFact(
        identifier="peak_margin",
        display_name="Peak Margin",
        description="Intraday peak margin obligation across snapshots during the trading day.",
        data_type=FactDataType.PERCENTAGE,
        allowed_units=frozenset({"%", "pct", "percent", "per cent", "percentage", "inr", "inr crore", "inr lakh", "crore", "lakh"}),
        min_value=0.0,
        max_value=100.0,
        synonyms=(
            "peak margin",
            "minimum peak margin",
            "peak margin requirement",
            "peak margin collection",
            "intraday peak margin",
            "intra day peak margin",
            "peak intraday margin",
            "peak margin percentage",
            "peak margin pct",
            "peak margin obligation",
            "peak margin collection requirement",
        ),
        regex_patterns=(
            re.compile(r"^(?:minimum\s+|mandatory\s+)?(?:intraday\s+|intra\s*day\s+)?peak\s+margin(?:\s+percentage|\s+pct|\s+requirement|\s+collection|\s+obligation)?$"),
        ),
    ),
    "collateral_reporting": CanonicalFact(
        identifier="collateral_reporting",
        display_name="Collateral Reporting",
        description="Cadence, frequency, or timeframe for reporting client collateral to clearing corporations or exchanges.",
        data_type=FactDataType.DURATION_DAYS,
        allowed_units=frozenset({"days", "day", "hours", "hour", "months", "month", "times", "t+1", "t+2", "daily"}),
        min_value=0.0,
        max_value=None,
        synonyms=(
            "collateral reporting",
            "client collateral reporting",
            "margin reporting",
            "collateral reporting frequency",
            "reporting of collateral",
            "collateral report cadence",
            "collateral reporting timeline",
            "daily collateral reporting",
            "reporting of client collateral",
            "margin reporting timeline",
            "collateral reporting schedule",
        ),
        regex_patterns=(
            re.compile(r"^(?:client\s+|daily\s+)?(?:collateral|margin)\s+reporting(?:\s+frequency|\s+timeline|\s+cadence|\s+schedule)?$"),
            re.compile(r"^reporting\s+of\s+(?:client\s+)?collateral$"),
        ),
    ),
    "reporting_deadline": CanonicalFact(
        identifier="reporting_deadline",
        display_name="Reporting Deadline",
        description="Maximum permissible timeframe or statutory deadline for regulatory or compliance reporting.",
        data_type=FactDataType.DURATION_DAYS,
        allowed_units=frozenset({"days", "day", "hours", "hour", "months", "month", "weeks", "week"}),
        min_value=0.0,
        max_value=None,
        synonyms=(
            "reporting deadline",
            "deadline for reporting",
            "reporting timeline",
            "submission deadline",
            "compliance reporting deadline",
            "filing deadline",
            "reporting timeframe",
            "time limit for reporting",
            "report submission deadline",
            "statutory reporting deadline",
            "regulatory reporting deadline",
            "deadline for submission",
        ),
        regex_patterns=(
            re.compile(r"^(?:compliance\s+|statutory\s+|regulatory\s+|report\s+)?(?:reporting\s+deadline|filing\s+deadline|submission\s+deadline)$"),
            re.compile(r"^(?:deadline|timeline|timeframe)\s+for\s+(?:reporting|submission|filing)$"),
        ),
    ),
    # Supporting baseline financial/regulatory metrics already used in repo
    "net_worth_inr_crore": CanonicalFact(
        identifier="net_worth_inr_crore",
        display_name="Net Worth (INR Crore)",
        description="Minimum net worth requirement in INR crore.",
        data_type=FactDataType.CURRENCY,
        allowed_units=frozenset({"inr crore", "crore", "crores", "inr lakh", "lakh", "inr", "rs"}),
        min_value=0.0,
        max_value=None,
        synonyms=(
            "net worth",
            "minimum net worth",
            "net worth requirement",
            "networth",
            "minimum networth",
        ),
        regex_patterns=(
            re.compile(r"^(?:minimum\s+)?net\s*worth(?:\s+requirement)?$"),
        ),
    ),
    "settlement_window_days": CanonicalFact(
        identifier="settlement_window_days",
        display_name="Settlement Window (Days)",
        description="Rolling trade settlement window in days (e.g. T+1, T+2).",
        data_type=FactDataType.DURATION_DAYS,
        allowed_units=frozenset({"days", "day", "hours"}),
        min_value=0.0,
        max_value=None,
        synonyms=(
            "settlement window",
            "settlement cycle",
            "settlement period",
            "trade settlement window",
        ),
        regex_patterns=(
            re.compile(r"^(?:trade\s+)?settlement\s+(?:window|cycle|period)$"),
        ),
    ),
    "crar_pct": CanonicalFact(
        identifier="crar_pct",
        display_name="Capital to Risk-Weighted Assets Ratio",
        description="CRAR percentage for regulated entities.",
        data_type=FactDataType.PERCENTAGE,
        allowed_units=frozenset({"%", "pct", "percent", "per cent"}),
        min_value=0.0,
        max_value=100.0,
        synonyms=(
            "crar",
            "capital adequacy ratio",
            "capital to risk weighted assets ratio",
        ),
        regex_patterns=(),
    ),
    "single_borrower_exposure_pct": CanonicalFact(
        identifier="single_borrower_exposure_pct",
        display_name="Single Borrower Exposure Percentage",
        description="Maximum exposure limit to a single borrower as percentage of capital.",
        data_type=FactDataType.PERCENTAGE,
        allowed_units=frozenset({"%", "pct", "percent", "per cent"}),
        min_value=0.0,
        max_value=100.0,
        synonyms=(
            "single borrower exposure",
            "single borrower limit",
            "single borrower exposure limit",
        ),
        regex_patterns=(),
    ),
    "solvency_ratio": CanonicalFact(
        identifier="solvency_ratio",
        display_name="Solvency Ratio",
        description="Solvency margin ratio for insurance/regulated entities.",
        data_type=FactDataType.RATIO,
        allowed_units=frozenset({"ratio", "times", "%", "pct"}),
        min_value=0.0,
        max_value=None,
        synonyms=(
            "solvency ratio",
            "solvency margin",
            "minimum solvency ratio",
        ),
        regex_patterns=(),
    ),
    "equity_exposure_pct": CanonicalFact(
        identifier="equity_exposure_pct",
        display_name="Equity Exposure Percentage",
        description="Maximum equity exposure limit percentage.",
        data_type=FactDataType.PERCENTAGE,
        allowed_units=frozenset({"%", "pct", "percent", "per cent"}),
        min_value=0.0,
        max_value=100.0,
        synonyms=(
            "equity exposure",
            "maximum equity exposure",
            "equity exposure limit",
        ),
        regex_patterns=(),
    ),
}


@dataclass(frozen=True)
class FactResolutionResult:
    status: FactValidationStatus
    canonical_identifier: str | None = None
    canonical_fact: CanonicalFact | None = None
    error_message: str | None = None

    @property
    def is_valid(self) -> bool:
        return self.status == FactValidationStatus.VALID and self.canonical_identifier is not None


def get_canonical_fact(identifier: str) -> CanonicalFact | None:
    """Retrieve a CanonicalFact definition by its exact identifier."""
    return _CANONICAL_FACTS.get(identifier)


def get_all_canonical_facts() -> dict[str, CanonicalFact]:
    """Return dictionary of all registered canonical facts."""
    return dict(_CANONICAL_FACTS)


def resolve_canonical_fact(
    metric: str,
    unit: str | None = None,
    value: float | None = None,
    value_upper: float | None = None,
) -> FactResolutionResult:
    """Maps a human-readable or LLM-extracted metric name to a canonical fact identifier,
    validating unit and threshold constraints using deterministic Python logic.
    """
    if not metric or not str(metric).strip():
        return FactResolutionResult(
            status=FactValidationStatus.MISSING_MAPPING,
            error_message="Metric name is empty or missing.",
        )

    clean_metric = normalize_metric_text(str(metric))

    # 1. Direct match on canonical identifier
    matched_fact: CanonicalFact | None = None
    metric_str = str(metric).strip()
    if metric_str in _CANONICAL_FACTS:
        matched_fact = _CANONICAL_FACTS[metric_str]
    elif clean_metric in _CANONICAL_FACTS:
        matched_fact = _CANONICAL_FACTS[clean_metric]

    # 2. Match synonyms and regex patterns across the taxonomy
    if matched_fact is None:
        for fact in _CANONICAL_FACTS.values():
            if clean_metric in fact.synonyms:
                matched_fact = fact
                break
            # Check pattern match
            if any(pattern.match(clean_metric) for pattern in fact.regex_patterns):
                matched_fact = fact
                break

    if matched_fact is None:
        return FactResolutionResult(
            status=FactValidationStatus.MISSING_MAPPING,
            error_message=(
                f"Metric '{metric}' does not map to any canonical regulatory fact. "
                "Arbitrary field name invention is prohibited."
            ),
        )

    # 3. Unit Validation (if unit provided)
    if unit is not None and str(unit).strip():
        unit_str = str(unit)
        norm_unit = normalize_unit(unit_str)
        raw_unit_lower = unit_str.strip().lower()
        if norm_unit not in matched_fact.allowed_units and raw_unit_lower not in matched_fact.allowed_units:
            allowed = sorted(list(matched_fact.allowed_units))
            return FactResolutionResult(
                status=FactValidationStatus.WRONG_UNIT,
                canonical_identifier=matched_fact.identifier,
                canonical_fact=matched_fact,
                error_message=(
                    f"Unit '{unit}' is invalid for canonical fact '{matched_fact.identifier}'. "
                    f"Allowed units: {allowed}."
                ),
            )

    # 4. Threshold Range Validation (if value provided)
    if value is not None:
        if matched_fact.min_value is not None and value < matched_fact.min_value:
            return FactResolutionResult(
                status=FactValidationStatus.WRONG_THRESHOLD,
                canonical_identifier=matched_fact.identifier,
                canonical_fact=matched_fact,
                error_message=(
                    f"Threshold value {value} is below minimum permissible value "
                    f"{matched_fact.min_value} for '{matched_fact.identifier}'."
                ),
            )
        if matched_fact.max_value is not None and value > matched_fact.max_value:
            return FactResolutionResult(
                status=FactValidationStatus.WRONG_THRESHOLD,
                canonical_identifier=matched_fact.identifier,
                canonical_fact=matched_fact,
                error_message=(
                    f"Threshold value {value} exceeds maximum permissible value "
                    f"{matched_fact.max_value} for '{matched_fact.identifier}'."
                ),
            )

    if value_upper is not None:
        if matched_fact.min_value is not None and value_upper < matched_fact.min_value:
            return FactResolutionResult(
                status=FactValidationStatus.WRONG_THRESHOLD,
                canonical_identifier=matched_fact.identifier,
                canonical_fact=matched_fact,
                error_message=(
                    f"Upper threshold value {value_upper} is below minimum permissible value "
                    f"{matched_fact.min_value} for '{matched_fact.identifier}'."
                ),
            )
        if matched_fact.max_value is not None and value_upper > matched_fact.max_value:
            return FactResolutionResult(
                status=FactValidationStatus.WRONG_THRESHOLD,
                canonical_identifier=matched_fact.identifier,
                canonical_fact=matched_fact,
                error_message=(
                    f"Upper threshold value {value_upper} exceeds maximum permissible value "
                    f"{matched_fact.max_value} for '{matched_fact.identifier}'."
                ),
            )

    return FactResolutionResult(
        status=FactValidationStatus.VALID,
        canonical_identifier=matched_fact.identifier,
        canonical_fact=matched_fact,
    )


def compute_canonical_facts_digest(thresholds_or_facts: list[Any] | None) -> str:
    """Computes a deterministic SHA-256 digest over normalized canonical facts
    and numeric thresholds associated with a regulatory clause or rule.
    """
    import hashlib
    import json

    if not thresholds_or_facts:
        return hashlib.sha256(b"[]").hexdigest()

    records = []
    for item in thresholds_or_facts:
        if isinstance(item, dict):
            metric = item.get("metric", "")
            cf = item.get("canonical_fact") or item.get("canonical_identifier") or ""
            op = str(item.get("operator", ""))
            val = item.get("value")
            val_upper = item.get("value_upper")
            unit = item.get("unit", "")
        else:
            metric = getattr(item, "metric", "")
            cf = getattr(item, "canonical_fact", None) or getattr(item, "canonical_identifier", "") or ""
            op = str(getattr(item, "operator", ""))
            val = getattr(item, "value", None)
            val_upper = getattr(item, "value_upper", None)
            unit = getattr(item, "unit", "")

        records.append({
            "metric": str(metric),
            "canonical_fact": str(cf) if cf else "",
            "operator": op,
            "value": float(val) if val is not None else None,
            "value_upper": float(val_upper) if val_upper is not None else None,
            "unit": normalize_unit(str(unit)) if unit else "",
        })

    # Sort records by canonical_fact, metric, operator, value for strict canonical ordering
    records.sort(key=lambda r: (r["canonical_fact"], r["metric"], r["operator"], str(r["value"])))
    payload = json.dumps(records, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

