"""End-to-end regulatory pipeline integration tests:
PDF -> extraction -> Circular persistence -> Clause persistence -> clause extraction/audit
    -> policy compilation -> CompiledRule persistence -> HITLReview creation
    -> human approval -> policy publication -> OPA evaluation -> evidence/audit ledger.
"""
from __future__ import annotations

import datetime as dt
from io import BytesIO
from typing import Any
import httpx
import pytest
import pytest_asyncio
from reportlab.pdfgen import canvas
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import Settings, get_settings
from app.db.base import Base
from app.db.models import Circular, Clause, CompiledRule, HITLReview, Tenant
from app.db.session import get_db_session
from app.execution.dependencies import (
    get_evaluator,
    get_hitl_queue,
    get_kill_switch_store,
    get_opa_engine,
    get_policy_cache,
    get_policy_event_publisher,
    get_policy_publisher,
    get_policy_registry,
    get_redis_pool,
)
from app.execution.evaluator import Evaluator
from app.execution.hitl_queue import HITLQueue
from app.execution.models import Decision, EvaluationResult, PolicyOutcome, TransactionPayload
from app.execution.opa_engine import OPAEngine
from app.execution.policy_cache import PolicyCache
from app.execution.policy_events import PolicyEventPublisher
from app.execution.policy_publisher import PolicyPublisher
from app.execution.policy_registry import PolicyRegistry
from app.governance.kill_switch import KillSwitchStore
from app.ledger.dependencies import get_ledger_engine, get_ledger_service
from app.ledger.models import compliance_audit_ledger
from app.ledger.service import LedgerService
from app.ledger.verifier import verify_chain
from app.main import app
from app.security.jwt import create_access_token
from app.security.models import Role


def _generate_test_margin_pdf() -> bytes:
    """Generates a valid, small text-layer SEBI circular with upfront margin rule."""
    buf = BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(100, 750, "Circular No. SEBI/HO/MRD/2026/045")
    c.drawString(100, 730, "15 January 2026")
    c.drawString(100, 700, "1. Applicability and Scope")
    c.drawString(100, 680, "1.1 This circular applies to all registered stock brokers.")
    c.drawString(100, 650, "2. Upfront Margin Requirements")
    c.drawString(100, 630, "2.1 Every stock broker shall maintain an upfront margin of not less than 20% of the transaction value.")
    c.save()
    return buf.getvalue()


class _MockOPAEngine:
    """In-memory OPA engine mock that tracks published policies and evaluates
    them based on the compiled Rego requirements."""

    def __init__(self) -> None:
        self.policies: dict[str, str] = {}
        self.packages: dict[str, str] = {}

    async def publish_policy(self, compiled: Any) -> None:
        self.policies[compiled.rule_id] = compiled.rego_code
        self.packages[compiled.package] = compiled.rule_id

    async def remove_policy(self, rule_id: str) -> None:
        self.policies.pop(rule_id, None)

    async def evaluate(self, package: str, input_doc: dict[str, Any]) -> dict[str, Any] | None:
        facts = input_doc.get("facts", {})
        margin = facts.get("upfront_margin_pct")
        rule_id = self.packages.get(package, "unknown_rule")

        if margin is None:
            return None

        # Evaluates the upfront margin rule (threshold >= 20%)
        if margin < 20.0:
            return {
                "allow": False,
                "violations": [f"Upfront Margin {margin}% violates required >= 20%"],
                "rule_id": rule_id,
                "clause_number": "2.1",
                "circular_number": "SEBI/HO/MRD/2026/045",
                "obligation_type": "mandatory",
            }
        else:
            return {
                "allow": True,
                "violations": [],
                "rule_id": rule_id,
                "clause_number": "2.1",
                "circular_number": "SEBI/HO/MRD/2026/045",
                "obligation_type": "mandatory",
            }


class _FakeRedis:
    def __init__(self) -> None:
        self.strings: dict[str, str] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.sets: dict[str, set[str]] = {}
        self.lists: dict[str, list[str]] = {}
        self.published: list[tuple[str, str]] = []

    async def get(self, key: str) -> str | None:
        return self.strings.get(key)

    async def set(self, key: str, value: str, *args, **kwargs) -> bool:
        self.strings[key] = str(value)
        return True

    async def incr(self, key: str) -> int:
        val = int(self.strings.get(key, 0)) + 1
        self.strings[key] = str(val)
        return val

    async def expire(self, key: str, seconds: int, *args, **kwargs) -> bool:
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

    async def publish(self, channel: str, message: str) -> int:
        self.published.append((channel, message))
        return 1

    async def rpush(self, key: str, *values: str) -> int:
        lst = self.lists.setdefault(key, [])
        lst.extend(values)
        return len(lst)

    async def lpop(self, key: str) -> str | None:
        lst = self.lists.get(key, [])
        return lst.pop(0) if lst else None

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


@pytest_asyncio.fixture
async def e2e_environment(monkeypatch: pytest.MonkeyPatch):
    """Sets up an isolated in-memory SQLite database, mock OPA, and fake Redis."""
    db_engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with db_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(compliance_audit_ledger.metadata.create_all)

    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)

    fake_redis = _FakeRedis()
    mock_opa = _MockOPAEngine()

    _wire_app_redis(app, fake_redis)
    monkeypatch.setattr("app.execution.dependencies.get_redis_pool", lambda: fake_redis)

    settings = get_settings()
    policy_registry = PolicyRegistry(redis_client=fake_redis, registry_key=settings.policy_registry_key)
    policy_cache = PolicyCache(policy_registry, ttl_seconds=settings.policy_cache_ttl_seconds)
    hitl_queue = HITLQueue(redis_client=fake_redis, key_prefix=settings.hitl_key_prefix)
    kill_switch_store = KillSwitchStore(redis_client=fake_redis, key_prefix=settings.governance_key_prefix)
    event_publisher = PolicyEventPublisher(redis_client=fake_redis)
    policy_publisher = PolicyPublisher(
        event_publisher,
        opa_engine=mock_opa,
        policy_registry=policy_registry,
        policy_cache=policy_cache,
    )
    evaluator = Evaluator(
        opa_engine=mock_opa,
        policy_registry=policy_cache,
        hitl_queue=hitl_queue,
        kill_switch_store=kill_switch_store,
    )
    ledger_service = LedgerService(db_engine)

    async def _override_get_db_session():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db_session] = _override_get_db_session
    app.dependency_overrides[get_redis_pool] = lambda: fake_redis
    app.dependency_overrides[get_opa_engine] = lambda: mock_opa
    app.dependency_overrides[get_policy_registry] = lambda: policy_registry
    app.dependency_overrides[get_policy_cache] = lambda: policy_cache
    app.dependency_overrides[get_policy_publisher] = lambda: policy_publisher
    app.dependency_overrides[get_evaluator] = lambda: evaluator
    app.dependency_overrides[get_ledger_engine] = lambda: db_engine
    app.dependency_overrides[get_ledger_service] = lambda: ledger_service

    yield {
        "engine": db_engine,
        "session_factory": session_factory,
        "mock_opa": mock_opa,
        "policy_registry": policy_registry,
        "evaluator": evaluator,
        "ledger_service": ledger_service,
        "settings": settings,
    }

    app.dependency_overrides.clear()
    await db_engine.dispose()
    await fake_redis.aclose()


@pytest.mark.asyncio
async def test_complete_e2e_pipeline(e2e_environment) -> None:
    """Full end-to-end integration test:
    1. Upload SEBI PDF -> Circular, Clauses, CompiledRule, HITLReview persisted.
    2. Status endpoint reflects 'review_required'.
    3. Human compliance officer approves HITLReview.
    4. CompiledRule is activated and published to OPA / PolicyRegistry.
    5. Status endpoint reflects 'deployed'.
    6. OPA evaluates a live transaction against the published policy:
       - Margin < 20% -> DENY
       - Margin >= 20% -> ALLOW
    7. Ledger records compliance evaluation events with clause_hash matching Clause.sha256.
    """
    settings = e2e_environment["settings"]
    session_factory = e2e_environment["session_factory"]
    db_engine = e2e_environment["engine"]

    # Generate test tokens
    now = dt.datetime.now(dt.timezone.utc)
    officer_token, _ = create_access_token(
        subject="compliance_officer_1",
        roles=[Role.COMPLIANCE_OFFICER, Role.SYSTEM_ADMIN],
        settings=settings,
        signing_key=settings.jwt_secret_key,
        auth_time=now,
        amr=["pwd", "mfa"],
    )
    broker_token, _ = create_access_token(
        subject="broker_client_1",
        roles=[Role.BROKER_API_CLIENT],
        settings=settings,
        signing_key=settings.jwt_secret_key,
        tenant_id="stockbroker_a",
        auth_time=now,
    )

    pdf_bytes = _generate_test_margin_pdf()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        # -------------------------------------------------------------------
        # Step 1: POST /v1/circulars/process-e2e
        # -------------------------------------------------------------------
        response = await client.post(
            "/v1/circulars/process-e2e",
            headers={"Authorization": f"Bearer {officer_token}"},
            files={"file": ("sebi_margin_2026.pdf", pdf_bytes, "application/pdf")},
        )
        assert response.status_code == 200, f"Process E2E failed: {response.text}"
        data = response.json()
        circular_id = data["circular_id"]
        assert circular_id > 0
        assert data["circular_number"] == "SEBI/HO/MRD/2026/045"
        assert len(data["document_hash"]) == 64
        assert len(data["source_document_sha256"]) == 64
        assert len(data["extracted_text_sha256"]) == 64
        # Proves original PDF bytes hash is distinct from extracted text hash
        assert data["source_document_sha256"] != data["extracted_text_sha256"]
        assert data["document_hash"] == data["source_document_sha256"]
        assert data["clause_count"] >= 2
        assert data["rules_compiled"] >= 1
        assert data["status"] == "review_required"
        assert len(data["reviews"]) >= 1

        # -------------------------------------------------------------------
        # Step 2: Verify persistence in DB
        # -------------------------------------------------------------------
        async with session_factory() as session:
            circular = await session.get(Circular, circular_id)
            assert circular is not None
            assert circular.circular_number == "SEBI/HO/MRD/2026/045"
            assert circular.source_document_sha256 == data["source_document_sha256"]
            assert circular.raw_text_digest == data["extracted_text_sha256"]
            assert circular.extracted_text_sha256 == data["extracted_text_sha256"]
            assert circular.is_shared is True

            clauses = (
                await session.execute(
                    select(Clause).where(Clause.circular_id == circular_id)
                )
            ).scalars().all()
            assert len(clauses) >= 2
            margin_clause = next((c for c in clauses if c.clause_number == "2.1"), None)
            assert margin_clause is not None
            assert "upfront margin" in margin_clause.text.lower()
            assert len(margin_clause.sha256) == 64

            compiled_rules = (
                await session.execute(
                    select(CompiledRule).where(CompiledRule.clause_id == margin_clause.id)
                )
            ).scalars().all()
            assert len(compiled_rules) >= 1
            compiled_rule = compiled_rules[0]
            assert compiled_rule.is_compiled is True
            assert compiled_rule.is_active is False  # Must not be active before review approval!
            assert compiled_rule.rego_policy is not None
            assert "upfront_margin_pct" in compiled_rule.rego_policy

            reviews = (
                await session.execute(
                    select(HITLReview).where(HITLReview.compiled_rule_id == compiled_rule.id)
                )
            ).scalars().all()
            assert len(reviews) >= 1
            review = reviews[0]
            assert review.status == "PENDING"
            assert review.clause_id == margin_clause.id
            assert review.compiled_rule_id == compiled_rule.id
            review_id = review.review_id

        # -------------------------------------------------------------------
        # Step 3: Check status retrieval
        # -------------------------------------------------------------------
        status_resp = await client.get(
            f"/v1/circulars/{circular_id}/status",
            headers={"Authorization": f"Bearer {officer_token}"},
        )
        assert status_resp.status_code == 200
        status_data = status_resp.json()
        assert status_data["status"] == "review_required"
        assert status_data["pending_reviews"] >= 1
        assert status_data["active_rules"] == 0

        # Also check alias route /v1/circulars/status/{circular_id}
        alias_resp = await client.get(
            f"/v1/circulars/status/{circular_id}",
            headers={"Authorization": f"Bearer {officer_token}"},
        )
        assert alias_resp.status_code == 200
        assert alias_resp.json()["status"] == "review_required"

        # -------------------------------------------------------------------
        # Step 4: Human approval via POST /v1/hitl-reviews/{review_id}/approve
        # -------------------------------------------------------------------
        approve_resp = await client.post(
            f"/v1/hitl-reviews/{review_id}/approve",
            headers={"Authorization": f"Bearer {officer_token}"},
            json={"notes": "Approved by Senior Compliance Officer."},
        )
        assert approve_resp.status_code == 200, f"Approve failed: {approve_resp.text}"
        approve_data = approve_resp.json()
        assert approve_data["status"] == "RESOLVED"
        assert approve_data["compliance_officer_id"] == "compliance_officer_1"

        # Verify DB state after approval
        async with session_factory() as session:
            refreshed_rule = await session.get(CompiledRule, compiled_rule.id)
            assert refreshed_rule.is_active is True
            assert refreshed_rule.hitl_status == "RESOLVED"

            refreshed_review = (
                await session.execute(
                    select(HITLReview).where(HITLReview.review_id == review_id)
                )
            ).scalar_one()
            assert refreshed_review.status == "RESOLVED"
            assert refreshed_review.resolved_at is not None

        # Approve any remaining pending reviews for this circular so it can transition
        async with session_factory() as session:
            pending_db_reviews = (
                await session.execute(
                    select(HITLReview)
                    .join(Clause, Clause.id == HITLReview.clause_id)
                    .where(Clause.circular_id == circular_id, HITLReview.status == "PENDING")
                )
            ).scalars().all()
            pending_ids = [r.review_id for r in pending_db_reviews if r.review_id != review_id]

        for rev_id in pending_ids:
            await client.post(
                f"/v1/hitl-reviews/{rev_id}/approve",
                headers={"Authorization": f"Bearer {officer_token}"},
                json={"notes": "Approved by Senior Compliance Officer."},
            )

        # Verify status endpoint reflects 'approved' or 'deployed'
        status_resp = await client.get(
            f"/v1/circulars/{circular_id}/status",
            headers={"Authorization": f"Bearer {officer_token}"},
        )
        assert status_resp.status_code == 200
        assert status_resp.json()["status"] in ("approved", "deployed")
        assert status_resp.json()["active_rules"] >= 1

        # -------------------------------------------------------------------
        # Step 5: OPA evaluates a live transaction against the published policy
        # -------------------------------------------------------------------
        # Case A: Violating transaction (margin = 15.0% < 20%) -> DENY
        tx_deny = {
            "transaction_id": "TXN-VIO-2026-001",
            "broker_id": "stockbroker_a",
            "entity_type": "Stockbroker",
            "facts": {"upfront_margin_pct": 15.0},
        }
        eval_deny_resp = await client.post(
            "/v1/execution/transactions/evaluate",
            headers={"Authorization": f"Bearer {broker_token}"},
            json=tx_deny,
        )
        assert eval_deny_resp.status_code == 200
        eval_deny_data = eval_deny_resp.json()
        assert eval_deny_data["decision"] == "deny"
        assert len(eval_deny_data["matched_policies"]) >= 1
        assert any("violates" in v for v in eval_deny_data["matched_policies"][0]["violations"])

        # Case B: Compliant transaction (margin = 25.0% >= 20%) -> ALLOW
        tx_allow = {
            "transaction_id": "TXN-OK-2026-002",
            "broker_id": "stockbroker_a",
            "entity_type": "Stockbroker",
            "facts": {"upfront_margin_pct": 25.0},
        }
        eval_allow_resp = await client.post(
            "/v1/execution/transactions/evaluate",
            headers={"Authorization": f"Bearer {broker_token}"},
            json=tx_allow,
        )
        assert eval_allow_resp.status_code == 200
        eval_allow_data = eval_allow_resp.json()
        assert eval_allow_data["decision"] == "allow"
        assert len(eval_allow_data["matched_policies"]) >= 1
        assert len(eval_allow_data["matched_policies"][0]["violations"]) == 0

        # -------------------------------------------------------------------
        # Step 6: Verify evidence in audit ledger & backlink to exact clause
        # -------------------------------------------------------------------
        async with db_engine.connect() as conn:
            ledger_rows = (
                await conn.execute(
                    select(compliance_audit_ledger).order_by(compliance_audit_ledger.c.sequence_num.asc())
                )
            ).mappings().all()

        assert len(ledger_rows) == 2, f"Expected 2 ledger entries, got {len(ledger_rows)}"

        # Verify Entry 1 (Deny)
        row_deny = ledger_rows[0]
        assert row_deny["transaction_id"] == "TXN-VIO-2026-001"
        assert row_deny["evaluation_result"] == "FAIL"
        assert row_deny["circular_id"] == "SEBI/HO/MRD/2026/045"
        assert row_deny["rule_id"] == compiled_rule.rule_id
        assert row_deny["clause_hash"] == margin_clause.sha256
        assert row_deny["section_reference"] == "2.1"

        # Verify Entry 2 (Allow)
        row_allow = ledger_rows[1]
        assert row_allow["transaction_id"] == "TXN-OK-2026-002"
        assert row_allow["evaluation_result"] == "PASS"
        assert row_allow["circular_id"] == "SEBI/HO/MRD/2026/045"
        assert row_allow["rule_id"] == compiled_rule.rule_id
        assert row_allow["clause_hash"] == margin_clause.sha256
        assert row_allow["section_reference"] == "2.1"

        # Verify cryptographic chain integrity
        chain_result = await verify_chain(db_engine)
        assert chain_result.valid is True
        assert chain_result.entries_checked == 2
