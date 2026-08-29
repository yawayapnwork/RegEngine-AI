"""Resolves a compliance team's period shorthand -- a SEBI/Indian
financial-year quarter ("Q2-2026") or a full financial year ("FY2025-26")
-- into a concrete `app.analytics.models.ReportPeriod`, so the reporting
CLI's collectors and `app.analytics.aggregator.ComplianceAggregator` both
work off the exact same date window.

India's financial year runs April 1 - March 31 (NOT the calendar year),
and SEBI's own quarterly reporting cadence follows it:
    Q1 = Apr-Jun, Q2 = Jul-Sep, Q3 = Oct-Dec, Q4 = Jan-Mar (of the FOLLOWING calendar year)
"FY2025-26" means April 2025 - March 2026. "Q2-2026" is ambiguous without
this convention fixed -- this module fixes it as "the Q2 that falls
within FY2025-26", i.e. Jul-Sep 2025, NOT Jul-Sep 2026; see
`resolve_quarter`'s docstring for the exact rule.
"""
from __future__ import annotations

import datetime as dt
import re

from app.analytics.models import Granularity, ReportPeriod

_QUARTER_MONTH_RANGES: dict[int, tuple[int, int]] = {
    1: (4, 6),
    2: (7, 9),
    3: (10, 12),
    4: (1, 3),
}

_QUARTER_RE = re.compile(r"^Q([1-4])-(\d{4})$", re.IGNORECASE)
_FY_RE = re.compile(r"^FY(\d{4})-(\d{2})$", re.IGNORECASE)


def _last_day_of_month(year: int, month: int) -> int:
    if month == 12:
        next_month_first = dt.date(year + 1, 1, 1)
    else:
        next_month_first = dt.date(year, month + 1, 1)
    return (next_month_first - dt.timedelta(days=1)).day


def resolve_quarter(spec: str) -> ReportPeriod:
    """`spec` is `"Q<1-4>-<FY-start-calendar-year>"`, e.g. `"Q2-2025"` for
    the Jul-Sep quarter of FY2025-26 (Apr 2025 - Mar 2026). Q4 spans into
    the FOLLOWING calendar year (Jan-Mar 2026 for FY2025-26's Q4) -- this
    is the one quarter where the spec's year and the quarter's actual
    calendar year differ, by design, since it's still part of the FY that
    STARTED in the given year.
    """
    match = _QUARTER_RE.match(spec.strip())
    if not match:
        raise ValueError(f"Invalid quarter spec {spec!r}; expected 'Q<1-4>-<FY start year>', e.g. 'Q2-2025'.")
    quarter_num = int(match.group(1))
    fy_start_year = int(match.group(2))

    start_month, end_month = _QUARTER_MONTH_RANGES[quarter_num]
    calendar_year_start = fy_start_year if quarter_num != 4 else fy_start_year + 1
    calendar_year_end = calendar_year_start

    start_date = dt.date(calendar_year_start, start_month, 1)
    end_date = dt.date(calendar_year_end, end_month, _last_day_of_month(calendar_year_end, end_month))

    return ReportPeriod(start_date=start_date, end_date=end_date, granularity=Granularity.QUARTERLY)


def resolve_fiscal_year(spec: str) -> ReportPeriod:
    """`spec` is `"FY<start-year>-<2-digit-end-year>"`, e.g.
    `"FY2025-26"` for April 1 2025 - March 31 2026. The two years must be
    consecutive (`FY2025-26`, never `FY2025-27`) -- SEBI financial years
    are always exactly 12 months."""
    match = _FY_RE.match(spec.strip())
    if not match:
        raise ValueError(f"Invalid financial-year spec {spec!r}; expected 'FY<start year>-<2-digit end year>', e.g. 'FY2025-26'.")
    start_year = int(match.group(1))
    end_year_short = int(match.group(2))
    expected_end_short = (start_year + 1) % 100
    if end_year_short != expected_end_short:
        raise ValueError(f"Invalid financial-year spec {spec!r}: {start_year}-{end_year_short:02d} is not a consecutive 12-month FY.")

    start_date = dt.date(start_year, 4, 1)
    end_date = dt.date(start_year + 1, 3, 31)
    return ReportPeriod(start_date=start_date, end_date=end_date, granularity=Granularity.QUARTERLY)


def resolve_period(*, quarter: str | None, fiscal_year: str | None, start: str | None, end: str | None) -> ReportPeriod:
    """Single entrypoint the CLI calls -- exactly one of
    (`quarter`) / (`fiscal_year`) / (`start`+`end`) must be provided;
    this is enforced here rather than relying on Typer's own mutually-
    exclusive-option support, which doesn't cleanly express a 3-way
    "exactly one of these input SHAPES" constraint (one option takes a
    single value, the others need two)."""
    provided = [bool(quarter), bool(fiscal_year), bool(start or end)]
    if sum(provided) != 1:
        raise ValueError("Specify exactly one of: --quarter, --fiscal-year, or --start/--end.")

    if quarter:
        return resolve_quarter(quarter)
    if fiscal_year:
        return resolve_fiscal_year(fiscal_year)

    if not (start and end):
        raise ValueError("--start and --end must both be supplied together.")
    return ReportPeriod(start_date=dt.date.fromisoformat(start), end_date=dt.date.fromisoformat(end), granularity=Granularity.MONTHLY)
