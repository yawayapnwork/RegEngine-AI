"""Statistical anomaly detection for compliance telemetry.

Algorithm
---------
For each metric series (fail_rate_pct, hitl_rate_pct, pass_rate_pct,
total_transactions), we compute a rolling mean and standard deviation over
the preceding N buckets (default: all available history, minimum 3).  A
data point is flagged as anomalous if its z-score exceeds the configured
threshold.

  z = (observed - rolling_mean) / rolling_std

Severity bands:
  LOW   2σ ≤ z < 3σ   — noteworthy, include in report as advisory
  HIGH  z ≥ 3σ         — flag prominently; may indicate a systematic issue

Per-broker anomalies are detected independently of system-wide ones — a
spike that affects only one intermediary is more actionable than a
system-wide move and deserves its own callout.

Why rolling z-score and not Isolation Forest / Prophet?
  The ledger series is typically short (12–16 monthly buckets per year)
  and must be explainable to a SEBI auditor without a statistics doctorate.
  A z-score on a rolling window is deterministic, reproducible from the
  same data, and has an intuitive plain-English explanation ("the denial
  rate was 3.2 standard deviations above its recent average").
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Sequence

import numpy as np
import pandas as pd

from app.analytics.models import (
    AggregatedReport,
    AnomalyEvent,
    AnomalySeverity,
    AnomalyType,
    PeriodBucket,
)

logger = logging.getLogger(__name__)

# Default z-score thresholds
_Z_HIGH = 3.0
_Z_LOW = 2.0
# Minimum number of historical data points needed to compute a stable std.
_MIN_HISTORY = 3


def _zscore_series(values: pd.Series) -> pd.Series:
    """Return a rolling z-score series.

    Uses all available preceding points (expanding window) so earlier
    buckets get z=0 (insufficient history) and later buckets get
    increasingly stable scores.  This avoids false positives from a
    fixed window being too short in the first few months.
    """
    mean = values.expanding(min_periods=_MIN_HISTORY).mean()
    std = values.expanding(min_periods=_MIN_HISTORY).std(ddof=1)
    z = (values - mean) / std.replace(0, np.nan)
    return z.fillna(0.0)


def _classify_severity(z: float) -> AnomalySeverity | None:
    az = abs(z)
    if az >= _Z_HIGH:
        return AnomalySeverity.HIGH
    if az >= _Z_LOW:
        return AnomalySeverity.LOW
    return None


def _bucket_series_to_df(buckets: Sequence[PeriodBucket]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "period_label": b.period_label,
                "total_transactions": b.total_transactions,
                "pass_rate_pct": b.pass_rate_pct,
                "fail_rate_pct": b.fail_rate_pct,
                "hitl_rate_pct": b.hitl_rate_pct,
            }
            for b in buckets
        ]
    )


# Map metric name → AnomalyType
_METRIC_TO_TYPE: dict[str, AnomalyType] = {
    "fail_rate_pct": AnomalyType.DENIAL_SPIKE,
    "hitl_rate_pct": AnomalyType.HITL_SPIKE,
    "pass_rate_pct": AnomalyType.PASS_RATE_DROP,
    "total_transactions": AnomalyType.VOLUME_SPIKE,
}

_METRIC_HUMAN: dict[str, str] = {
    "fail_rate_pct": "Denial Rate (%)",
    "hitl_rate_pct": "HITL Review Rate (%)",
    "pass_rate_pct": "Pass Rate (%)",
    "total_transactions": "Transaction Volume",
}


def _detect_in_series(
    df: pd.DataFrame,
    broker_id: str | None,
) -> list[AnomalyEvent]:
    """Run z-score detection across all four metrics for a single broker
    (or None for system-wide) on the provided time-series DataFrame."""
    if len(df) < _MIN_HISTORY:
        # Not enough history to produce meaningful z-scores.
        return []

    events: list[AnomalyEvent] = []
    for metric, atype in _METRIC_TO_TYPE.items():
        if metric not in df.columns:
            continue
        series = df[metric].astype(float)
        z_scores = _zscore_series(series)

        for idx, (z, obs) in enumerate(zip(z_scores, series)):
            severity = _classify_severity(float(z))
            if severity is None:
                continue

            period_label = str(df.iloc[idx]["period_label"])

            # Compute the baseline mean/std up to this point for the report
            history = series.iloc[: idx + 1]
            baseline_mean = float(history[:-1].mean()) if len(history) > 1 else 0.0
            baseline_std = float(history[:-1].std(ddof=1)) if len(history) > 1 else 0.0

            # Directional description
            direction = "spike" if z > 0 else "drop"
            description = (
                f"{_METRIC_HUMAN[metric]} {direction} detected in period {period_label}: "
                f"observed {obs:.2f}, baseline mean {baseline_mean:.2f} "
                f"(z={z:.2f}, {severity.value} severity)."
            )
            if broker_id:
                description = f"[{broker_id}] " + description

            events.append(
                AnomalyEvent(
                    anomaly_type=atype,
                    severity=severity,
                    broker_id=broker_id,
                    period_label=period_label,
                    metric_name=_METRIC_HUMAN[metric],
                    observed_value=round(float(obs), 4),
                    baseline_mean=round(baseline_mean, 4),
                    baseline_std=round(baseline_std, 4),
                    z_score=round(float(z), 4),
                    description=description,
                    detected_at=dt.datetime.now(dt.timezone.utc),
                )
            )

    return events


class AnomalyDetector:
    """Runs z-score anomaly detection across a completed ``AggregatedReport``.

    Designed to be called *after* ``ComplianceAggregator.build_aggregated_report``
    so it operates on already-assembled Pydantic objects rather than raw SQL.

    Usage::

        detector = AnomalyDetector(z_high=3.0, z_low=2.0)
        anomalies = detector.detect(report)
        report.anomalies = anomalies
        report.anomaly_count_high = sum(1 for a in anomalies if a.severity == AnomalySeverity.HIGH)
        report.anomaly_count_low  = sum(1 for a in anomalies if a.severity == AnomalySeverity.LOW)
    """

    def __init__(self, z_high: float = _Z_HIGH, z_low: float = _Z_LOW) -> None:
        global _Z_HIGH, _Z_LOW
        _Z_HIGH = z_high
        _Z_LOW = z_low

    def detect(self, report: AggregatedReport) -> list[AnomalyEvent]:
        """Detect anomalies across the system-wide time series and each broker's
        individual series. Returns a deduplicated, severity-sorted list."""
        all_events: list[AnomalyEvent] = []

        # 1. System-wide anomalies (aggregate time series)
        if len(report.time_series) >= _MIN_HISTORY:
            global_df = _bucket_series_to_df(report.time_series)
            all_events.extend(_detect_in_series(global_df, broker_id=None))

        # 2. Per-broker anomalies
        # Build per-broker time-series from broker_stats cross-referenced
        # with time_series data.  Since broker_stats aggregates the full
        # period (not buckets), we reconstruct per-broker buckets from the
        # time_series data where available; otherwise use the single aggregate
        # as a single-point series (insufficient for z-score, skipped naturally
        # by the _MIN_HISTORY guard inside _detect_in_series).
        #
        # NOTE: A production system might store per-broker per-bucket metrics
        # in a materialised view; here we generate per-broker anomalies from
        # the broker-level summary treated as a "period of 1" which only
        # produces system-wide results.  The per-broker rate values are still
        # checked against the system-wide baseline for contextual comparison.
        for broker in report.broker_stats:
            if broker.total_transactions < 10:
                # Too few transactions to be statistically meaningful.
                continue

            # Compare each broker's rates against the system-wide mean for the period.
            # This flags outlier brokers rather than temporal spikes.
            broker_events = self._detect_broker_vs_system(
                broker_fail_rate=broker.fail_rate_pct,
                broker_hitl_rate=broker.hitl_rate_pct,
                broker_pass_rate=broker.pass_rate_pct,
                broker_id=broker.broker_id,
                system_time_series=report.time_series,
                period_label=report.period.label(),
            )
            all_events.extend(broker_events)

        # Sort: HIGH severity first, then by z-score magnitude descending
        all_events.sort(
            key=lambda e: (0 if e.severity == AnomalySeverity.HIGH else 1, -abs(e.z_score))
        )

        logger.info(
            "Anomaly detection complete: report_id=%s total=%d high=%d low=%d",
            report.report_id,
            len(all_events),
            sum(1 for e in all_events if e.severity == AnomalySeverity.HIGH),
            sum(1 for e in all_events if e.severity == AnomalySeverity.LOW),
        )
        return all_events

    def _detect_broker_vs_system(
        self,
        broker_fail_rate: float,
        broker_hitl_rate: float,
        broker_pass_rate: float,
        broker_id: str,
        system_time_series: list[PeriodBucket],
        period_label: str,
    ) -> list[AnomalyEvent]:
        """Flag a broker whose rates deviate significantly from the system mean."""
        if not system_time_series:
            return []

        sys_df = _bucket_series_to_df(system_time_series)
        events: list[AnomalyEvent] = []

        for metric, broker_val, atype in [
            ("fail_rate_pct", broker_fail_rate, AnomalyType.DENIAL_SPIKE),
            ("hitl_rate_pct", broker_hitl_rate, AnomalyType.HITL_SPIKE),
            ("pass_rate_pct", broker_pass_rate, AnomalyType.PASS_RATE_DROP),
        ]:
            if metric not in sys_df.columns or sys_df[metric].empty:
                continue

            sys_mean = float(sys_df[metric].mean())
            sys_std = float(sys_df[metric].std(ddof=1))

            if sys_std == 0 or np.isnan(sys_std):
                continue

            z = (broker_val - sys_mean) / sys_std
            severity = _classify_severity(z)
            if severity is None:
                continue

            direction = "above" if z > 0 else "below"
            events.append(
                AnomalyEvent(
                    anomaly_type=atype,
                    severity=severity,
                    broker_id=broker_id,
                    period_label=period_label,
                    metric_name=_METRIC_HUMAN[metric],
                    observed_value=round(broker_val, 4),
                    baseline_mean=round(sys_mean, 4),
                    baseline_std=round(sys_std, 4),
                    z_score=round(z, 4),
                    description=(
                        f"Broker {broker_id} {_METRIC_HUMAN[metric]} ({broker_val:.2f}%) "
                        f"is {abs(z):.2f}σ {direction} the system average ({sys_mean:.2f}%)."
                    ),
                    detected_at=dt.datetime.now(dt.timezone.utc),
                )
            )
        return events


def detect_anomalies(report: AggregatedReport) -> AggregatedReport:
    """Convenience wrapper: run detection and mutate ``report`` in place.

    Returns the same report object (mutated) so callers can chain::

        report = detect_anomalies(await aggregator.build_aggregated_report(...))
    """
    detector = AnomalyDetector()
    anomalies = detector.detect(report)
    report.anomalies = anomalies
    report.anomaly_count_high = sum(1 for a in anomalies if a.severity == AnomalySeverity.HIGH)
    report.anomaly_count_low = sum(1 for a in anomalies if a.severity == AnomalySeverity.LOW)
    return report
