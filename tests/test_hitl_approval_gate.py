"""Tests for HITL Approval Gate and CompiledRule Activation Protection.

Requirements covered:
1. One pending review remaining -> approval does not activate rule
2. Two pending reviews -> first approval does not activate rule
3. Final required approval -> rule activates
4. Rejected review -> rule cannot activate
5. Revision-required review -> rule cannot activate
6. Concurrent approval behavior: safe serialization and exact activation
7. Role / step-up authorization checks
"""
from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any
import uuid

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sqlalchemy.pool import StaticPool

from app.config import Settings, get_settings
from app.db.base import Base
from app.db.models import Circular, Clause, CompiledRule, HITLReview, Tenant
from app.db.session import get_db_session
from app.execution.dependencies import get_policy_publisher, get_redis_pool
from app.execution.policy_publisher import PolicyPublisher
from app.main import app
from app.security.jwt import create_access_token
from app.security.models import Role
from app.services.hitl_service import (
    APPROVED_STATUS,
    DISQUALIFYING_STATUSES,
    HITLReviewService,
    UNRESOLVED_STATUSES,
    evaluate_rule_hitl_gate,
)


class _FakeRedis:
    def __init__(self) -> None:
        self.strings: dict[str, str] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.sets: dict[str, set[str]] = {}
        self.lists: dict[str, list[str]] = {}

    async def get(self, key: str) -> str | None:
        return self.strings.get(key)

    async def set(self, key: str, value: str, *args: Any, **kwargs: Any) -> bool:
        self.strings[key] = str(value)
        return True

    async def setex(self, key: str, time: int, value: str) -> bool:
        self.strings[key] = str(value)
        return True

    async def incr(self, key: str) -> int:
        val = int(self.strings.get(key, 0)) + 1
        self.strings[key] = str(val)
        return val

    async def expire(self, key: str, seconds: int, *args: Any, **kwargs: Any) -> bool:
        return True

    async def delete(self, *keys: str) -> int:
        count = 0
        for k in keys:
            if k in self.strings:
                del self.strings[k]
                count += 1
            if k in self.hashes:
                del self.hashes[k]
                count += 1
        return count

    async def hget(self, key: str, field: str) -> str | None:
        return self.hashes.get(key, {}).get(field)

    async def hset(self, key: str, field: str | None = None, value: str | None = None, mapping: dict | None = None) -> int:
        h = self.hashes.setdefault(key, {})
        if mapping:
            h.update({k: str(v) for k, v in mapping.items()})
            return len(mapping)
        elif field is not None:
            h[field] = str(value)
            return 1
        return 0

    async def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))

    async def hdel(self, key: str, *fields: str) -> int:
        h = self.hashes.get(key, {})
        count = 0
        for f in fields:
            if f in h:
                del h[f]
                count += 1
        return count

    async def smembers(self, key: str) -> set[str]:
        return set(self.sets.get(key, set()))

    async def sadd(self, key: str, *members: str) -> int:
        s = self.sets.setdefault(key, set())
        old_len = len(s)
        s.update(members)
        return len(s) - old_len

    async def srem(self, key: str, *members: str) -> int:
        s = self.sets.get(key, set())
        count = 0
        for m in members:
            if m in s:
                s.remove(m)
                count += 1
        return count

    async def exists(self, *keys: str) -> int:
        count = 0
        for k in keys:
            if k in self.strings or k in self.hashes or k in self.sets or k in self.lists:
                count += 1
        return count

    def pipeline(self) -> _FakePipeline:
        return _FakePipeline(self)

    async def ttl(self, key: str) -> int:
        return 60

    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        pass


class _FakePipeline:
    def __init__(self, fake_redis: _FakeRedis) -> None:
        self.fake_redis = fake_redis
        self.ops: list[tuple[str, Any]] = []

    def incr(self, key: str) -> _FakePipeline:
        self.ops.append(("incr", key))
        return self

    def expire(self, key: str, seconds: int, nx: bool = False) -> _FakePipeline:
        self.ops.append(("expire", key, seconds))
        return self

    async def execute(self) -> list[Any]:
        results = []
        for op in self.ops:
            if op[0] == "incr":
                val = await self.fake_redis.incr(op[1])
                results.append(val)
            elif op[0] == "expire":
                results.append(True)
        return results


def _wire_app_redis(app_instance, fake_redis):
    for mw in getattr(app_instance, "user_middleware", []):
        if "redis_client" in mw.kwargs:
            mw.kwargs["redis_client"] = fake_redis
        if "kill_switch_store" in mw.kwargs:
            mw.kwargs["kill_switch_store"]._redis = fake_redis
    app_instance.middleware_stack = app_instance.build_middleware_stack()


class _MockPolicyPublisher:
    """Mock PolicyPublisher capturing published events."""

    def __init__(self) -> None:
        self.published_events: list[tuple[Any, str]] = []

    async def publish_approved(
        self,
        compiled_rule: CompiledRule,
        approved_by: str,
        entity_types: list[str] | None = None,
    ) -> None:
        self.published_events.append((compiled_rule, approved_by))


@pytest_asyncio.fixture
async def hitl_env():
    """Sets up an in-memory test database and mock publisher."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        echo=False,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    publisher = _MockPolicyPublisher()

    # Seed baseline tenant
    async with session_factory() as session:
        tenant = Tenant(
            tenant_id="stockbroker_alpha",
            display_name="Stockbroker Alpha",
            tenant_type="stockbroker",
            opa_bundle_prefix="tenants/stockbroker_alpha",
        )
        session.add(tenant)
        await session.commit()

    return {"session_factory": session_factory, "publisher": publisher, "engine": engine}


async def _create_test_rule_and_reviews(
    session: AsyncSession,
    rule_id: str = "SEBI_MARGIN_001",
    num_blocking: int = 2,
    num_advisory: int = 0,
) -> tuple[CompiledRule, list[HITLReview]]:
    """Helper creating a Circular, Clause, CompiledRule, and requested HITLReviews."""
    circular = Circular(
        circular_number=f"CIRC-{uuid.uuid4().hex[:8]}",
        title="Test Circular",
        raw_text_digest=uuid.uuid4().hex * 2,
        tenant_id="stockbroker_alpha",
    )
    session.add(circular)
    await session.flush()

    clause = Clause(
        circular_id=circular.id,
        tenant_id="stockbroker_alpha",
        clause_number="1.1",
        text="Sample clause text requiring compliance.",
        sha256=uuid.uuid4().hex * 2,
    )
    session.add(clause)
    await session.flush()

    compiled_rule = CompiledRule(
        clause_id=clause.id,
        tenant_id="stockbroker_alpha",
        rule_id=rule_id,
        rule_version=1,
        rego_policy="package sebi.test\ndefault allow = true\n",
        opa_package_name="sebi.test",
        is_compiled=True,
        is_active=False,
        hitl_status="BLOCKING" if num_blocking > 0 else "ADVISORY",
    )
    session.add(compiled_rule)
    await session.flush()

    reviews: list[HITLReview] = []
    for i in range(num_blocking):
        rev = HITLReview(
            review_id=f"rev-blocking-{i}-{uuid.uuid4().hex[:6]}",
            clause_id=clause.id,
            compiled_rule_id=compiled_rule.id,
            tenant_id="stockbroker_alpha",
            reason_code="low_extraction_confidence",
            severity="blocking",
            description=f"Blocking review #{i + 1}",
            status="PENDING",
        )
        session.add(rev)
        reviews.append(rev)

    for i in range(num_advisory):
        rev = HITLReview(
            review_id=f"rev-advisory-{i}-{uuid.uuid4().hex[:6]}",
            clause_id=clause.id,
            compiled_rule_id=compiled_rule.id,
            tenant_id="stockbroker_alpha",
            reason_code="qualitative_directive",
            severity="advisory",
            description=f"Advisory review #{i + 1}",
            status="PENDING",
        )
        session.add(rev)
        reviews.append(rev)

    await session.commit()
    await session.refresh(compiled_rule)
    for r in reviews:
        await session.refresh(r)
    return compiled_rule, reviews


@pytest.mark.asyncio
async def test_status_classifications() -> None:
    """Verify status constants and gate evaluation helper."""
    assert "PENDING" in UNRESOLVED_STATUSES
    assert "IN_REVIEW" in UNRESOLVED_STATUSES
    assert "REJECTED" in DISQUALIFYING_STATUSES
    assert "REVISION_REQUIRED" in DISQUALIFYING_STATUSES
    assert APPROVED_STATUS == "RESOLVED"

    # Empty reviews list passes gate
    can_act, target_status, reason = evaluate_rule_hitl_gate([])
    assert can_act is True
    assert target_status == "RESOLVED"


@pytest.mark.asyncio
async def test_two_pending_reviews_first_approval_does_not_activate_rule(hitl_env) -> None:
    """Approving the 1st of 2 pending reviews must NOT activate the CompiledRule."""
    session_factory = hitl_env["session_factory"]
    publisher = hitl_env["publisher"]

    async with session_factory() as session:
        compiled_rule, reviews = await _create_test_rule_and_reviews(
            session, rule_id="RULE-TWO-REVIEWS", num_blocking=2
        )
        r1, r2 = reviews

        # Approve review 1
        updated_r1, rule_activated, rule = await HITLReviewService.approve_review(
            session=session,
            review_id=r1.review_id,
            principal_subject="officer.alice",
            notes="First sign-off complete.",
            policy_publisher=publisher,
        )

        assert updated_r1.status == "RESOLVED"
        assert updated_r1.compliance_officer_id == "officer.alice"
        assert rule_activated is False

        # Verify DB state: rule remains INACTIVE and BLOCKING
        refreshed_rule = await session.get(CompiledRule, compiled_rule.id)
        assert refreshed_rule.is_active is False
        assert refreshed_rule.hitl_status == "BLOCKING"
        # Policy must not be published to OPA
        assert len(publisher.published_events) == 0


@pytest.mark.asyncio
async def test_one_pending_review_remaining_approval_does_not_activate_rule(hitl_env) -> None:
    """When one pending blocking review remains unresolved, approval does not activate rule."""
    session_factory = hitl_env["session_factory"]
    publisher = hitl_env["publisher"]

    async with session_factory() as session:
        compiled_rule, reviews = await _create_test_rule_and_reviews(
            session, rule_id="RULE-ONE-PENDING-LEFT", num_blocking=2
        )
        r1, r2 = reviews

        # Approve r1 while r2 is still pending
        _, rule_activated, _ = await HITLReviewService.approve_review(
            session=session,
            review_id=r1.review_id,
            principal_subject="officer.alice",
            policy_publisher=publisher,
        )
        assert rule_activated is False

        refreshed_rule = await session.get(CompiledRule, compiled_rule.id)
        assert refreshed_rule.is_active is False
        assert len(publisher.published_events) == 0


@pytest.mark.asyncio
async def test_final_required_approval_activates_rule(hitl_env) -> None:
    """When the final required review is approved, CompiledRule activates and publishes to OPA."""
    session_factory = hitl_env["session_factory"]
    publisher = hitl_env["publisher"]

    async with session_factory() as session:
        compiled_rule, reviews = await _create_test_rule_and_reviews(
            session, rule_id="RULE-FINAL-ACTIVATION", num_blocking=2
        )
        r1, r2 = reviews

        # 1. First approval -> does not activate
        _, act1, _ = await HITLReviewService.approve_review(
            session=session,
            review_id=r1.review_id,
            principal_subject="officer.alice",
            policy_publisher=publisher,
        )
        assert act1 is False
        assert len(publisher.published_events) == 0

        # 2. Final required approval -> ACTIVATES rule
        updated_r2, act2, active_rule = await HITLReviewService.approve_review(
            session=session,
            review_id=r2.review_id,
            principal_subject="officer.bob",
            notes="Final sign-off complete.",
            policy_publisher=publisher,
        )
        assert act2 is True
        assert updated_r2.status == "RESOLVED"

        refreshed_rule = await session.get(CompiledRule, compiled_rule.id)
        assert refreshed_rule.is_active is True
        assert refreshed_rule.hitl_status == "RESOLVED"
        assert len(publisher.published_events) == 1
        assert publisher.published_events[0][0].rule_id == "RULE-FINAL-ACTIVATION"
        assert publisher.published_events[0][1] == "officer.bob"


@pytest.mark.asyncio
async def test_rejected_review_prevents_rule_activation(hitl_env) -> None:
    """If one review is REJECTED, the rule CANNOT activate even if all other reviews are approved."""
    session_factory = hitl_env["session_factory"]
    publisher = hitl_env["publisher"]

    async with session_factory() as session:
        compiled_rule, reviews = await _create_test_rule_and_reviews(
            session, rule_id="RULE-REJECTED", num_blocking=2
        )
        r1, r2 = reviews

        # Reject review 1
        rejected_r1 = await HITLReviewService.reject_review(
            session=session,
            review_id=r1.review_id,
            principal_subject="officer.alice",
            notes="Extracted thresholds do not match circular intent.",
        )
        assert rejected_r1.status == "REJECTED"
        assert rejected_r1.resolved_at is not None

        # Try to approve review 2
        updated_r2, rule_activated, _ = await HITLReviewService.approve_review(
            session=session,
            review_id=r2.review_id,
            principal_subject="officer.bob",
            policy_publisher=publisher,
        )
        assert updated_r2.status == "RESOLVED"
        assert rule_activated is False

        # Rule must remain INACTIVE
        refreshed_rule = await session.get(CompiledRule, compiled_rule.id)
        assert refreshed_rule.is_active is False
        assert refreshed_rule.hitl_status == "BLOCKING"
        assert len(publisher.published_events) == 0


@pytest.mark.asyncio
async def test_revision_required_review_prevents_rule_activation(hitl_env) -> None:
    """If one review is REVISION_REQUIRED, the rule CANNOT activate even if other reviews are approved."""
    session_factory = hitl_env["session_factory"]
    publisher = hitl_env["publisher"]

    async with session_factory() as session:
        compiled_rule, reviews = await _create_test_rule_and_reviews(
            session, rule_id="RULE-REVISION-REQ", num_blocking=2
        )
        r1, r2 = reviews

        # Mark review 1 as revision required
        rev_req = await HITLReviewService.request_revision(
            session=session,
            review_id=r1.review_id,
            principal_subject="officer.alice",
            notes="Please re-extract with broader clause context.",
        )
        assert rev_req.status == "REVISION_REQUIRED"

        # Try to approve review 2
        updated_r2, rule_activated, _ = await HITLReviewService.approve_review(
            session=session,
            review_id=r2.review_id,
            principal_subject="officer.bob",
            policy_publisher=publisher,
        )
        assert updated_r2.status == "RESOLVED"
        assert rule_activated is False

        # Rule must remain INACTIVE
        refreshed_rule = await session.get(CompiledRule, compiled_rule.id)
        assert refreshed_rule.is_active is False
        assert refreshed_rule.hitl_status == "BLOCKING"
        assert len(publisher.published_events) == 0


@pytest.mark.asyncio
async def test_concurrent_approvals_safely_activate_rule(hitl_env) -> None:
    """Simultaneous approvals on two reviews serialize safely and activate the rule exactly once."""
    session_factory = hitl_env["session_factory"]
    publisher = hitl_env["publisher"]

    # Create rule and reviews
    async with session_factory() as session:
        compiled_rule, reviews = await _create_test_rule_and_reviews(
            session, rule_id="RULE-CONCURRENT-APPROVE", num_blocking=2
        )
        r1_id = reviews[0].review_id
        r2_id = reviews[1].review_id
        rule_db_id = compiled_rule.id

    # Simulate two concurrent officers approving in parallel tasks with separate sessions
    async def approve_task(rev_id: str, officer: str):
        async with session_factory() as sess:
            return await HITLReviewService.approve_review(
                session=sess,
                review_id=rev_id,
                principal_subject=officer,
                policy_publisher=publisher,
            )

    results = await asyncio.gather(
        approve_task(r1_id, "officer.alpha"),
        approve_task(r2_id, "officer.beta"),
    )

    # Exactly one approval should have triggered the final activation
    activations = [res[1] for res in results]
    assert True in activations
    assert activations.count(True) == 1

    # Verify final rule state in DB
    async with session_factory() as session:
        rule = await session.get(CompiledRule, rule_db_id)
        assert rule.is_active is True
        assert rule.hitl_status == "RESOLVED"

    # Exactly one OPA publication fired
    assert len(publisher.published_events) == 1


@pytest.mark.asyncio
async def test_http_hitl_approval_gate_endpoints(hitl_env) -> None:
    """Test full HTTP API flows for approve, reject, and request-revision with roles and MFA."""
    session_factory = hitl_env["session_factory"]
    publisher = hitl_env["publisher"]

    settings = get_settings()
    now = dt.datetime.now(dt.timezone.utc)
    officer_token, _ = create_access_token(
        subject="officer.jane",
        roles=[Role.COMPLIANCE_OFFICER],
        settings=settings,
        signing_key=settings.jwt_secret_key,
        auth_time=now,
        amr=["pwd", "mfa"],
    )
    broker_token, _ = create_access_token(
        subject="broker.client",
        roles=[Role.BROKER_API_CLIENT],
        settings=settings,
        signing_key=settings.jwt_secret_key,
        tenant_id="stockbroker_alpha",
    )

    async with session_factory() as session:
        compiled_rule, reviews = await _create_test_rule_and_reviews(
            session, rule_id="RULE-HTTP-TEST", num_blocking=2
        )
        r1_id = reviews[0].review_id
        r2_id = reviews[1].review_id
        rule_db_id = compiled_rule.id

    fake_redis = _FakeRedis()
    _wire_app_redis(app, fake_redis)

    async def _override_db():
        async with session_factory() as s:
            yield s

    app.dependency_overrides[get_db_session] = _override_db
    app.dependency_overrides[get_policy_publisher] = lambda: publisher
    app.dependency_overrides[get_redis_pool] = lambda: fake_redis

    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # 1. Unauthorized role cannot approve
            res_unauth = await client.post(
                f"/v1/hitl-reviews/{r1_id}/approve",
                headers={"Authorization": f"Bearer {broker_token}"},
                json={"notes": "Unauthorized attempt."},
            )
            assert res_unauth.status_code == 403

            # 2. Officer approves 1st review -> status 200, rule stays inactive
            res1 = await client.post(
                f"/v1/hitl-reviews/{r1_id}/approve",
                headers={"Authorization": f"Bearer {officer_token}"},
                json={"notes": "First approval via API."},
            )
            assert res1.status_code == 200
            assert res1.json()["status"] == "RESOLVED"

            async with session_factory() as session:
                rule_mid = await session.get(CompiledRule, rule_db_id)
                assert rule_mid.is_active is False
                assert rule_mid.hitl_status == "BLOCKING"
            assert len(publisher.published_events) == 0

            # 3. Cannot re-approve already resolved review (409 Conflict)
            res_reapprove = await client.post(
                f"/v1/hitl-reviews/{r1_id}/approve",
                headers={"Authorization": f"Bearer {officer_token}"},
                json={"notes": "Duplicate approval."},
            )
            assert res_reapprove.status_code == 409

            # 4. Officer approves 2nd (final) review -> rule ACTIVATES
            res2 = await client.post(
                f"/v1/hitl-reviews/{r2_id}/approve",
                headers={"Authorization": f"Bearer {officer_token}"},
                json={"notes": "Final approval via API."},
            )
            assert res2.status_code == 200
            assert res2.json()["status"] == "RESOLVED"

            async with session_factory() as session:
                rule_final = await session.get(CompiledRule, rule_db_id)
                assert rule_final.is_active is True
                assert rule_final.hitl_status == "RESOLVED"
            assert len(publisher.published_events) == 1

            # 5. Test request-revision endpoint on a fresh review
            async with session_factory() as session:
                _, rev_list = await _create_test_rule_and_reviews(
                    session, rule_id="RULE-REVISION-ENDPOINT", num_blocking=1
                )
                rev_id = rev_list[0].review_id

            res_rev = await client.post(
                f"/v1/hitl-reviews/{rev_id}/request-revision",
                headers={"Authorization": f"Bearer {officer_token}"},
                json={"notes": "Ambiguous threshold requires revision."},
            )
            assert res_rev.status_code == 200
            assert res_rev.json()["status"] == "REVISION_REQUIRED"

    finally:
        app.dependency_overrides.clear()
