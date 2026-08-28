"""Tests for app.observability.metrics: that each of the four required
metrics (plus the two supplementary ones needed for the Grafana dashboard)
actually appears in a real Prometheus scrape with the right value/labels,
and that record_hallucination_findings only counts hallucination-type
findings, not every audit finding.
"""
from __future__ import annotations

import asyncio

import pytest

from app.agents.schemas import AuditFinding, FindingType, Severity
from app.observability.metrics import (
    HITL_REVIEW_QUEUE_DEPTH,
    LLM_AGENT_TASK_DURATION,
    LLM_HALLUCINATION_DETECTION_TOTAL,
    OPA_POLICY_EVALUATION_DURATION,
    REGISTRY,
    SEBI_CIRCULAR_INGESTION_LATENCY,
    TRANSACTION_EVALUATION_TOTAL,
    observe_ingestion_latency,
    observe_opa_evaluation,
    poll_queue_depths,
    record_hallucination_findings,
    render_latest,
)


def _metric_sample_value(metric_name: str, **labels) -> float | None:
    # NOTE: a Histogram/Counter's CollectorRegistry family is keyed by the
    # metric's BASE name (e.g. "sebi_circular_ingestion_latency_seconds"),
    # not the suffixed sample name ("..._count", "..._bucket", "..._total")
    # -- so this must scan every family's samples by sample.name, not
    # filter families by metric_name first (that would skip every
    # suffixed sample entirely).
    for family in REGISTRY.collect():
        for sample in family.samples:
            if sample.name == metric_name and all(sample.labels.get(k) == v for k, v in labels.items()):
                return sample.value
    return None


class TestRequiredMetricsExist:
    """The four literally-named metrics must scrape under exactly these
    names -- a typo here (e.g. a stray suffix) would silently break every
    Grafana panel and alert rule built against the spec'd name."""

    def test_sebi_circular_ingestion_latency_seconds_is_a_histogram(self):
        with observe_ingestion_latency():
            pass
        body, content_type = render_latest()
        text = body.decode("utf-8")
        assert "sebi_circular_ingestion_latency_seconds_bucket" in text
        assert "sebi_circular_ingestion_latency_seconds_count" in text
        assert content_type.startswith("text/plain")

    def test_llm_hallucination_detection_total_is_a_counter(self):
        LLM_HALLUCINATION_DETECTION_TOTAL.labels(finding_type="hallucinated_threshold", severity="blocker").inc()
        text = render_latest()[0].decode("utf-8")
        assert "llm_hallucination_detection_total" in text

    def test_opa_policy_evaluation_duration_seconds_is_a_histogram(self):
        outcome = {"outcome": "allow"}
        with observe_opa_evaluation(outcome):
            pass
        text = render_latest()[0].decode("utf-8")
        assert "opa_policy_evaluation_duration_seconds_bucket" in text

    def test_hitl_review_queue_depth_is_a_gauge(self):
        HITL_REVIEW_QUEUE_DEPTH.labels(queue_type="transaction").set(3)
        assert _metric_sample_value("hitl_review_queue_depth", queue_type="transaction") == 3

    def test_supplementary_llm_agent_task_duration_and_transaction_outcome_metrics_exist(self):
        LLM_AGENT_TASK_DURATION.labels(audit_verdict="approved").observe(1.5)
        TRANSACTION_EVALUATION_TOTAL.labels(decision="deny").inc()
        text = render_latest()[0].decode("utf-8")
        assert "llm_agent_task_duration_seconds_bucket" in text
        assert "transaction_evaluation_total" in text


class TestIngestionLatencyOutcomeLabel:
    def test_success_is_labeled_success(self):
        before = _metric_sample_value("sebi_circular_ingestion_latency_seconds_count", outcome="success") or 0
        with observe_ingestion_latency():
            pass
        after = _metric_sample_value("sebi_circular_ingestion_latency_seconds_count", outcome="success")
        assert after == before + 1

    def test_exception_is_labeled_error_and_still_propagates(self):
        before = _metric_sample_value("sebi_circular_ingestion_latency_seconds_count", outcome="error") or 0
        with pytest.raises(ValueError):
            with observe_ingestion_latency():
                raise ValueError("boom")
        after = _metric_sample_value("sebi_circular_ingestion_latency_seconds_count", outcome="error")
        assert after == before + 1


class TestOpaEvaluationOutcomeLabel:
    def test_outcome_holder_value_is_used_as_the_label(self):
        before = _metric_sample_value("opa_policy_evaluation_duration_seconds_count", outcome="deny") or 0
        outcome = {}
        with observe_opa_evaluation(outcome):
            outcome["outcome"] = "deny"
        after = _metric_sample_value("opa_policy_evaluation_duration_seconds_count", outcome="deny")
        assert after == before + 1

    def test_unset_outcome_defaults_to_error(self):
        before = _metric_sample_value("opa_policy_evaluation_duration_seconds_count", outcome="error") or 0
        outcome = {}
        with pytest.raises(RuntimeError):
            with observe_opa_evaluation(outcome):
                raise RuntimeError("OPA unreachable")  # caller never sets outcome["outcome"] on the failure path
        after = _metric_sample_value("opa_policy_evaluation_duration_seconds_count", outcome="error")
        assert after == before + 1


class TestRecordHallucinationFindings:
    def test_counts_only_hallucination_finding_types(self):
        findings = [
            AuditFinding(finding_type=FindingType.HALLUCINATED_THRESHOLD, severity=Severity.BLOCKER, field_path="x", description="d"),
            AuditFinding(finding_type=FindingType.HALLUCINATED_ENTITY, severity=Severity.MAJOR, field_path="y", description="d"),
            AuditFinding(finding_type=FindingType.MISSING_CONTEXT, severity=Severity.MINOR, field_path="z", description="d"),
            AuditFinding(finding_type=FindingType.OK, severity=Severity.INFO, field_path="w", description="d"),
        ]

        count = record_hallucination_findings(findings)

        assert count == 2

    def test_empty_findings_counts_zero(self):
        assert record_hallucination_findings([]) == 0


@pytest.mark.asyncio
class TestPollQueueDepths:
    async def test_updates_transaction_gauge_from_redis_and_stops_cleanly(self):
        class _FakeRedis:
            async def scard(self, key: str) -> int:
                assert key == "regengine:hitl:pending"
                return 7

        class _FakeSessionCtx:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def execute(self, _query):
                class _Result:
                    def scalar_one(self_inner):
                        return 2

                return _Result()

        def _session_factory():
            return _FakeSessionCtx()

        stop_event = asyncio.Event()
        poll_task = asyncio.create_task(
            poll_queue_depths(
                redis_client=_FakeRedis(),
                hitl_pending_set_key="regengine:hitl:pending",
                db_session_factory=_session_factory,
                interval_seconds=0.01,
                stop_event=stop_event,
            )
        )
        await asyncio.sleep(0.03)
        stop_event.set()
        await asyncio.wait_for(poll_task, timeout=1.0)

        assert _metric_sample_value("hitl_review_queue_depth", queue_type="transaction") == 7
        assert _metric_sample_value("hitl_review_queue_depth", queue_type="policy") == 2

    async def test_a_failed_poll_does_not_kill_the_loop(self):
        calls = {"n": 0}

        class _FlakyRedis:
            async def scard(self, key: str) -> int:
                calls["n"] += 1
                if calls["n"] == 1:
                    raise ConnectionError("simulated Redis blip")
                return 5

        class _FakeSessionCtx:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def execute(self, _query):
                class _Result:
                    def scalar_one(self_inner):
                        return 0

                return _Result()

        stop_event = asyncio.Event()
        poll_task = asyncio.create_task(
            poll_queue_depths(
                redis_client=_FlakyRedis(),
                hitl_pending_set_key="k",
                db_session_factory=lambda: _FakeSessionCtx(),
                interval_seconds=0.01,
                stop_event=stop_event,
            )
        )
        await asyncio.sleep(0.05)  # first poll fails, second should succeed
        stop_event.set()
        await asyncio.wait_for(poll_task, timeout=1.0)

        assert calls["n"] >= 2
        assert _metric_sample_value("hitl_review_queue_depth", queue_type="transaction") == 5
