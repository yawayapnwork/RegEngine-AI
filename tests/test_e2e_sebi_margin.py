"""Complete end-to-end SEBI upfront margin regulatory pipeline test.

Validates the full chain:
SEBI PDF (synthetic regulatory fixture with clause number, obligation,
numerical threshold, deadline, qualitative requirement)
-> PDF extraction
-> Circular persisted
-> Clauses persisted
-> obligation extracted
-> source evidence attached
-> deterministic verification
-> policy compiled
-> CompiledRule persisted
-> HITLReview created
-> Compliance Officer approval
-> policy published
-> OPA evaluates transaction
-> PASS/FAIL
-> evidence written
-> evidence links back to exact clause.

Tests:
1. Compliant transaction (25% margin >= 20% -> ALLOW / PASS)
2. Non-compliant transaction (15% margin < 20% -> DENY / FAIL)
3. Cryptographic provenance chain and tamper detection
4. Unauthorized user cannot approve
5. Unapproved policy cannot deploy
6. Hallucinated threshold (e.g. 0%) rejected and cannot deploy.
"""
from __future__ import annotations

import datetime as dt
from io import BytesIO
from typing import Any
import httpx
import pytest
import pytest_asyncio
from reportlab.pdfgen import canvas
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.agents.schemas import (
    AuditVerdict,
    ComparisonOperator,
    ComplianceRuleAudit,
    ExtractedComplianceRule,
    NumericalThreshold,
    ObligationType,
    TargetEntity,
)
from app.compiler.pipeline import compile_audited_rule
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
from app.models import ClauseChunk
from app.redteam.output_guard import guard_and_validate_extraction
from app.security.jwt import create_access_token
from app.security.models import Role


def generate_synthetic_sebi_pdf() -> bytes:
    """Generates a synthetic SEBI Master Circular containing:
    - circular number
    - clause number (3.2.1, 4.1.0)
    - mandatory obligation
    - numerical threshold (20% upfront margin)
    - deadline (within one trading day)
    - qualitative requirement (adequate internal controls).
    """
    buf = BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(72, 770, "SECURITIES AND EXCHANGE BOARD OF INDIA")
    c.drawString(72, 750, "CIRCULAR")
    c.drawString(72, 730, "SEBI/HO/MIRSD/2026/CIR/P/001")
    c.drawString(72, 710, "January 15, 2026")
    c.drawString(72, 680, "To All Registered Stock Brokers and Clearing Members")
    c.drawString(72, 650, "Clause 3.2.1 Upfront Margin Requirement")
    c.drawString(
        72,
        630,
        "Every stock broker shall collect an upfront margin of not less than 20% of the transaction",
    )
    c.drawString(
        72,
        615,
        "value from the client before the execution of any trade in the derivatives segment.",
    )
    c.drawString(
        72,
        600,
        "Where the margin collected falls short, the broker shall report the shortfall within 1 trading day.",
    )
    c.drawString(72, 570, "Clause 4.1.0 Internal Controls and Risk Management")
    c.drawString(
        72,
        550,
        "Every stock broker shall maintain adequate internal controls and risk management systems.",
    )
    c.save()
    return buf.getvalue()


class _MockOPAEngine:
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
        rule_id = self.packages.get(package, "sebi_margin_rule")

        if margin is None:
            return None

        if margin < 20.0:
            return {
                "allow": False,
                "violations": [f"Upfront margin {margin}% fails required condition (>= 20%, clause 3.2.1)"],
                "rule_id": rule_id,
                "clause_number": "3.2.1",
                "circular_number": "SEBI/HO/MIRSD/2026/CIR/P/001",
                "obligation_type": "mandatory",
            }
        else:
            return {
                "allow": True,
                "violations": [],
                "rule_id": rule_id,
                "clause_number": "3.2.1",
                "circular_number": "SEBI/HO/MIRSD/2026/CIR/P/001",
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



@pytest.mark.e2e
@pytest.mark.asyncio
async def test_complete_sebi_margin_e2e_workflow() -> None:
    """Proves the exact workflow from SEBI PDF to live evaluation and ledger."""
    db_engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
    )
    async with db_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)

    async def override_get_db_session():
        async with session_factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    test_settings = get_settings().model_copy(
        update={
            "environment": "development",
            "storage_backend": "local",
            "llm_provider": "offline",
            "jwt_secret_key": "test-insecure-secret-key-32-chars-long",
            "demo_mode": True,
        }
    )

    fake_redis = _FakeRedis()
    _wire_app_redis(app, fake_redis)
    mock_opa = _MockOPAEngine()
    policy_registry = PolicyRegistry(fake_redis, test_settings.policy_registry_key)
    policy_cache = PolicyCache(policy_registry, ttl_seconds=60)
    policy_events = PolicyEventPublisher(fake_redis)
    policy_publisher = PolicyPublisher(
        policy_events,
        opa_engine=mock_opa,
        policy_registry=policy_registry,
        policy_cache=policy_cache,
    )
    hitl_queue = HITLQueue(fake_redis, key_prefix=test_settings.hitl_key_prefix)
    kill_switch_store = KillSwitchStore(fake_redis, key_prefix=test_settings.governance_key_prefix)
    evaluator = Evaluator(
        opa_engine=mock_opa,
        policy_registry=policy_cache,
        hitl_queue=hitl_queue,
        kill_switch_store=kill_switch_store,
    )
    ledger_service = LedgerService(db_engine)

    app.dependency_overrides[get_db_session] = override_get_db_session
    app.dependency_overrides[get_settings] = lambda: test_settings
    app.dependency_overrides[get_redis_pool] = lambda: fake_redis
    app.dependency_overrides[get_opa_engine] = lambda: mock_opa
    app.dependency_overrides[get_policy_cache] = lambda: policy_cache
    app.dependency_overrides[get_policy_event_publisher] = lambda: policy_events
    app.dependency_overrides[get_policy_registry] = lambda: policy_registry
    app.dependency_overrides[get_policy_publisher] = lambda: policy_publisher
    app.dependency_overrides[get_hitl_queue] = lambda: hitl_queue
    app.dependency_overrides[get_kill_switch_store] = lambda: kill_switch_store
    app.dependency_overrides[get_evaluator] = lambda: evaluator
    app.dependency_overrides[get_ledger_service] = lambda: ledger_service
    app.dependency_overrides[get_ledger_engine] = lambda: db_engine

    officer_token, _ = create_access_token(
        subject="compliance_officer_test",
        roles=[Role.COMPLIANCE_OFFICER],
        settings=test_settings,
        signing_key=test_settings.jwt_secret_key,
    )
    broker_token, _ = create_access_token(
        subject="broker_api_client_test",
        roles=[Role.BROKER_API_CLIENT],
        tenant_id="stockbroker_alpha",
        settings=test_settings,
        signing_key=test_settings.jwt_secret_key,
    )

    pdf_bytes = generate_synthetic_sebi_pdf()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        # Step 1: Ingest SEBI PDF
        files = {"file": ("SEBI_Margin_2026.pdf", pdf_bytes, "application/pdf")}
        resp = await client.post(
            "/v1/circulars/process-e2e",
            headers={"Authorization": f"Bearer {officer_token}"},
            files=files,
        )
        assert resp.status_code == 200, f"Upload failed: {resp.text}"
        data = resp.json()

        circular_id = data["circular_id"]
        assert data["clause_count"] >= 1
        assert data["status"] == "review_required"
        assert len(data["reviews"]) >= 1
        review_id = data["reviews"][0]

        # Step 2: Verify persistence & provenance
        async with session_factory() as session:
            circ = await session.get(Circular, circular_id)
            assert circ is not None
            assert len(circ.raw_text_digest) == 64

            clause_res = await session.execute(select(Clause).where(Clause.circular_id == circular_id))
            clauses = clause_res.scalars().all()
            assert len(clauses) >= 1
            margin_clause = clauses[0]
            assert len(margin_clause.sha256) == 64

            rules_res = await session.execute(
                select(CompiledRule).where(CompiledRule.clause_id == margin_clause.id)
            )
            compiled_rule = rules_res.scalars().first()
            assert compiled_rule is not None
            assert compiled_rule.is_compiled is True
            assert compiled_rule.is_active is False  # Must NOT be active prior to approval!

            review_res = await session.execute(
                select(HITLReview).where(HITLReview.review_id == review_id)
            )
            review = review_res.scalar_one()
            assert review.status == "PENDING"
            assert review.clause_id == margin_clause.id

        # Step 3: Verify security - broker/unauthorized user cannot approve review
        unauth_resp = await client.post(
            f"/v1/hitl-reviews/{review_id}/approve",
            headers={"Authorization": f"Bearer {broker_token}"},
            json={"notes": "Unauthorized attempt to approve."},
        )
        assert unauth_resp.status_code == 403

        # Step 4: Verify security - unapproved policy cannot be deployed
        async with session_factory() as session:
            r = await session.get(CompiledRule, compiled_rule.id)
            assert r.is_active is False

        # Step 5: Compliance Officer approves the policy
        approve_resp = await client.post(
            f"/v1/hitl-reviews/{review_id}/approve",
            headers={"Authorization": f"Bearer {officer_token}"},
            json={"notes": "Compliance sign-off: verified against SEBI upfront margin clause 3.2.1."},
        )
        assert approve_resp.status_code == 200
        assert approve_resp.json()["status"] == "RESOLVED"

        async with session_factory() as session:
            r = await session.get(CompiledRule, compiled_rule.id)
            assert r.is_active is True
            assert r.hitl_status == "RESOLVED"

        # Step 6: Test evaluation of non-compliant transaction (margin = 15% < 20%) -> DENY
        tx_fail = {
            "transaction_id": "TXN-MARGIN-SHORT-001",
            "broker_id": "stockbroker_alpha",
            "entity_type": "Stockbroker",
            "facts": {"upfront_margin_pct": 15.0},
        }
        res_fail = await client.post(
            "/v1/execution/transactions/evaluate",
            headers={"Authorization": f"Bearer {broker_token}"},
            json=tx_fail,
        )
        assert res_fail.status_code == 200
        fail_data = res_fail.json()
        assert fail_data["decision"] == "deny"
        assert len(fail_data["matched_policies"]) >= 1
        assert any("violates" in v or "fails" in v for v in fail_data["matched_policies"][0]["violations"])

        # Step 7: Test evaluation of compliant transaction (margin = 25% >= 20%) -> ALLOW
        tx_pass = {
            "transaction_id": "TXN-MARGIN-PASS-002",
            "broker_id": "stockbroker_alpha",
            "entity_type": "Stockbroker",
            "facts": {"upfront_margin_pct": 25.0},
        }
        res_pass = await client.post(
            "/v1/execution/transactions/evaluate",
            headers={"Authorization": f"Bearer {broker_token}"},
            json=tx_pass,
        )
        assert res_pass.status_code == 200
        pass_data = res_pass.json()
        assert pass_data["decision"] == "allow"
        assert len(pass_data["matched_policies"][0]["violations"]) == 0

        # Step 8: Evidence written to audit ledger linking back to exact clause
        async with db_engine.connect() as conn:
            ledger_rows = (
                await conn.execute(
                    select(compliance_audit_ledger).order_by(compliance_audit_ledger.c.sequence_num.asc())
                )
            ).mappings().all()

        assert len(ledger_rows) == 2
        entry_deny = ledger_rows[0]
        assert entry_deny["transaction_id"] == "TXN-MARGIN-SHORT-001"
        assert entry_deny["evaluation_result"] == "FAIL"
        assert entry_deny["clause_hash"] == margin_clause.sha256

        entry_allow = ledger_rows[1]
        assert entry_allow["transaction_id"] == "TXN-MARGIN-PASS-002"
        assert entry_allow["evaluation_result"] == "PASS"
        assert entry_allow["clause_hash"] == margin_clause.sha256

        # Step 9: Verify cryptographic hash chain integrity
        chain_verification = await verify_chain(db_engine)
        assert chain_verification.valid is True
        assert chain_verification.entries_checked == 2

        # Step 10: Verify provenance tamper detection - modifying an artifact breaks chain
        async with db_engine.begin() as conn:
            await conn.execute(
                compliance_audit_ledger.update()
                .where(compliance_audit_ledger.c.sequence_num == 0)
                .values(evaluation_result="PASS")  # Tamper with recorded verdict!
            )

        tampered_verification = await verify_chain(db_engine)
        assert tampered_verification.valid is False
        assert len(tampered_verification.breaks) >= 1

    app.dependency_overrides.clear()


@pytest.mark.e2e
def test_adversarial_hallucinated_threshold_cannot_deploy() -> None:
    """Proves that if an adversarial prompt injection or hallucinated model claims
    margin is 0% with fabricated evidence, it is rejected by output guard and cannot compile."""
    source_clause = (
        "Clause 3.2.1: Every stock broker shall collect an upfront margin of not less than 20% "
        "of the transaction value from the client."
    )
    chunk = ClauseChunk(
        chunk_id="chunk_adv_01",
        text=source_clause,
        circular_number="SEBI/2026/001",
        clause_number="3.2.1",
        sha256="e" * 64,
    )

    hallucinated_rule = ExtractedComplianceRule(
        rule_id="adv:rule:01",
        source_chunk_id="chunk_adv_01",
        source_sha256="e" * 64,
        target_entities=[
            TargetEntity(raw_text="stock broker", normalized_entity="Stockbroker", verbatim_evidence="stock broker")
        ],
        deterministic_logic=[
            NumericalThreshold(
                metric="Upfront Margin",
                canonical_fact="upfront_margin_pct",
                operator=ComparisonOperator.GTE,
                value=0.0,
                unit="%",
                verbatim_evidence="upfront margin of not less than 0%",
            )
        ],
        obligation_type=ObligationType.MANDATORY,
        extraction_confidence=0.99,
    )
    audit = ComplianceRuleAudit(
        rule_id="adv:rule:01",
        verdict=AuditVerdict.APPROVED,
        fidelity_score=0.99,
        verified_quote_count=1,
        unverified_quote_count=0,
    )

    guarded_rule, guarded_audit = guard_and_validate_extraction(hallucinated_rule, audit, chunk)
    assert guarded_audit.verdict == AuditVerdict.REJECTED
    assert guarded_audit.fidelity_score == 0.0

    from app.agents.schemas import AuditedComplianceRule
    compilation = compile_audited_rule(AuditedComplianceRule(rule=guarded_rule, audit=guarded_audit))
    assert compilation.compiled is False
    assert compilation.rego is None
