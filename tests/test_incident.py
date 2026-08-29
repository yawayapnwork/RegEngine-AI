"""Tests for the breach notification engine: trigger-matrix classification,
Redis-backed event storage, and escalation-policy stage timing.

Redis is faked throughout (see app/../tests/test_policy_cache_and_hot_reload.py's
module docstring for the same rationale) -- these test the actual
storage/escalation LOGIC, not network plumbing.
"""
from __future__ import annotations

import datetime as dt

import pytest

from app.config import get_settings
from app.execution.models import Decision, EvaluationResult, PolicyOutcome, SourceChannel, TransactionPayload
from app.incident.escalation_policy import build_escalation_policies
from app.incident.models import AckStatus, BreachEventType, Severity
from app.incident.store import BreachEventStore
from app.incident.trigger_matrix import ambiguous_hitl_event, clause_violation_event, policy_compiled_event


class _FakeRedis:
    """Implements just the subset of the redis.asyncio.Redis API
    BreachEventStore actually calls."""

    def __init__(self) -> None:
        self.strings: dict[str, str] = {}
        self.lists: dict[str, list[str]] = {}
        self.zsets: dict[str, dict[str, float]] = {}
        self.published: list[tuple[str, str]] = []

    async def set(self, key: str, value: str) -> None:
        self.strings[key] = value

    async def get(self, key: str) -> str | None:
        return self.strings.get(key)

    async def lpush(self, key: str, value: str) -> None:
        self.lists.setdefault(key, []).insert(0, value)

    async def ltrim(self, key: str, start: int, end: int) -> None:
        lst = self.lists.get(key, [])
        self.lists[key] = lst[start : end + 1]

    async def lrange(self, key: str, start: int, end: int) -> list[str]:
        lst = self.lists.get(key, [])
        return lst[start : end + 1 if end != -1 else None]

    async def zadd(self, key: str, mapping: dict[str, float]) -> None:
        self.zsets.setdefault(key, {}).update(mapping)

    async def zrem(self, key: str, member: str) -> None:
        self.zsets.get(key, {}).pop(member, None)

    async def zrange(self, key: str, start: int, end: int) -> list[str]:
        items = sorted(self.zsets.get(key, {}).items(), key=lambda kv: kv[1])
        members = [m for m, _ in items]
        return members[start : end + 1 if end != -1 else None]

    async def zrangebyscore(self, key: str, min: str, max: float) -> list[str]:
        items = self.zsets.get(key, {})
        return [m for m, score in items.items() if score <= max]

    async def publish(self, channel: str, message: str) -> None:
        self.published.append((channel, message))


def _transaction() -> TransactionPayload:
    return TransactionPayload(
        transaction_id="TXN-100", entity_type="Stockbroker", facts={}, broker_id="INZ0001001",
        source_channel=SourceChannel.REST_SYNC,
    )


class TestTriggerMatrix:
    def test_clause_violation_is_critical(self) -> None:
        transaction = _transaction()
        result = EvaluationResult(transaction_id="TXN-100", decision=Decision.DENY, evaluated_at=dt.datetime.now(dt.timezone.utc))
        outcome = PolicyOutcome(
            rule_id="a" * 64 + ":4.2.b", package="sebi.broking.circulars.x.clause_4_2_b",
            allow=False, violations=["Upfront Margin is 15 %, which fails the required condition (>= 20 %, clause 4.2.b)"],
            circular_number="SEBI/HO/MIRSD/DOP/CIR/P/2024/100", clause_number="4.2.b",
        )
        event = clause_violation_event(transaction, result, outcome)
        assert event.severity == Severity.CRITICAL
        assert event.event_type == BreachEventType.CLAUSE_VIOLATION
        assert event.requires_acknowledgment is True
        assert event.tenant_id == "INZ0001001"

    def test_ambiguous_hitl_is_warning(self) -> None:
        event = ambiguous_hitl_event(_transaction(), reason="undefined OPA result", hitl_case_id="case-1", rule_id="r1")
        assert event.severity == Severity.WARNING
        assert event.requires_acknowledgment is True
        assert event.hitl_case_id == "case-1"

    def test_policy_compiled_is_info_and_needs_no_ack(self) -> None:
        event = policy_compiled_event(rule_id="r1", circular_number="SEBI/HO/2024/1", clause_number="2.1", package="sebi.broking.circulars.x.clause_2_1")
        assert event.severity == Severity.INFO
        assert event.requires_acknowledgment is False


class TestEscalationPolicy:
    def test_critical_reaches_pagerduty_at_configured_deadline(self) -> None:
        settings = get_settings()
        stages = build_escalation_policies(settings)[Severity.CRITICAL]
        cumulative = 0
        for stage in stages:
            cumulative += stage.delay_seconds
        assert cumulative == settings.incident_critical_ack_deadline_seconds
        assert stages[-1].channels == ("pagerduty",)

    def test_info_has_no_escalation_stages(self) -> None:
        settings = get_settings()
        assert build_escalation_policies(settings)[Severity.INFO] == []


@pytest.mark.asyncio
class TestBreachEventStore:
    async def test_save_and_get_round_trips(self) -> None:
        redis_client = _FakeRedis()
        store = BreachEventStore(redis_client, "regengine:incidents")
        event = clause_violation_event(
            _transaction(),
            EvaluationResult(transaction_id="TXN-100", decision=Decision.DENY, evaluated_at=dt.datetime.now(dt.timezone.utc)),
            PolicyOutcome(rule_id="a" * 64 + ":4.2.b", package="pkg", allow=False, violations=["v"]),
        )
        await store.save(event)

        fetched = await store.get(event.event_id)
        assert fetched is not None
        assert fetched.event_id == event.event_id
        assert fetched.severity == Severity.CRITICAL

        recent = await store.list_recent()
        assert [e.event_id for e in recent] == [event.event_id]

        pending = await store.list_pending_ack()
        assert [e.event_id for e in pending] == [event.event_id]

    async def test_acknowledge_removes_from_pending_set(self) -> None:
        redis_client = _FakeRedis()
        store = BreachEventStore(redis_client, "regengine:incidents")
        event = clause_violation_event(
            _transaction(),
            EvaluationResult(transaction_id="TXN-100", decision=Decision.DENY, evaluated_at=dt.datetime.now(dt.timezone.utc)),
            PolicyOutcome(rule_id="a" * 64 + ":4.2.b", package="pkg", allow=False, violations=["v"]),
        )
        await store.save(event)

        acknowledged = await store.acknowledge(event.event_id, acknowledged_by="officer@example.com")
        assert acknowledged is not None
        assert acknowledged.ack_status == AckStatus.ACKNOWLEDGED
        assert acknowledged.acknowledged_by == "officer@example.com"

        pending = await store.list_pending_ack()
        assert pending == []

    async def test_info_event_never_enters_pending_ack_set(self) -> None:
        redis_client = _FakeRedis()
        store = BreachEventStore(redis_client, "regengine:incidents")
        event = policy_compiled_event(rule_id="r1", circular_number="c", clause_number="1", package="pkg")
        await store.save(event)

        pending = await store.list_pending_ack()
        assert pending == []
        recent = await store.list_recent()
        assert len(recent) == 1
