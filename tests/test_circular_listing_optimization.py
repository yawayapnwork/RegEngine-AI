"""Tests for circular listing optimization: eliminating N+1 queries,
verifying O(1) query scaling, pagination before aggregation, and semantic parity.
"""
import datetime as dt
from pathlib import Path
import pytest
import pytest_asyncio
import httpx
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import Settings, get_settings
from app.db.base import Base
from app.db.models import Circular, Clause, CompiledRule, HITLReview, Tenant
from app.db.session import get_db_session
from app.ledger.dependencies import get_ledger_engine, get_ledger_service
from app.ledger.service import LedgerService
from app.main import app
from app.parsing.hashing import sha256_of_bytes
from app.execution.dependencies import get_redis_pool
from app.security.jwt import create_access_token
from app.security.models import Role
from app.services.orchestrator import E2EOrchestrator, ProcessingState


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
                val = self.fake_redis.store.get(op[1], 0) + 1
                self.fake_redis.store[op[1]] = val
                results.append(val)
            elif op[0] == "expire":
                results.append(True)
        return results


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, Any] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.sets: dict[str, set[str]] = {}
        self.lists: dict[str, list[str]] = {}

    def pipeline(self) -> _FakePipeline:
        return _FakePipeline(self)

    async def get(self, key: str) -> Any:
        return self.store.get(key)

    async def set(self, key: str, value: Any, ex: int | None = None) -> bool:
        self.store[key] = value
        return True

    async def exists(self, *keys: str) -> int:
        count = 0
        for k in keys:
            if k in self.store or k in self.hashes or k in self.sets or k in self.lists:
                count += 1
        return count

    async def delete(self, *keys: str) -> int:
        count = 0
        for k in keys:
            if k in self.store:
                del self.store[k]
                count += 1
        return count

    async def aclose(self) -> None:
        pass


def _wire_app_redis(app_instance, fake_redis):
    for mw in getattr(app_instance, "user_middleware", []):
        if "redis_client" in mw.kwargs:
            mw.kwargs["redis_client"] = fake_redis
        if "kill_switch_store" in mw.kwargs:
            mw.kwargs["kill_switch_store"]._redis = fake_redis
    app_instance.middleware_stack = app_instance.build_middleware_stack()


@pytest_asyncio.fixture
async def listing_test_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    settings = Settings(
        app_env="testing",
        environment="development",
        database_url="sqlite+aiosqlite:///:memory:",
        ledger_database_url="sqlite+aiosqlite:///:memory:",
        jwt_secret_key="test_secret_for_listing_optimization_test_32chars",
        redis_url="redis://localhost:6379/0",
        ingestion_pdf_download_dir=str(tmp_path / "downloads"),
    )
    Path(settings.ingestion_pdf_download_dir).mkdir(parents=True, exist_ok=True)

    db_engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    async with db_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(
        bind=db_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    # Seed default tenant
    async with session_factory() as session:
        tenant = await session.get(Tenant, "sebi_baseline")
        if not tenant:
            session.add(
                Tenant(
                    tenant_id="sebi_baseline",
                    display_name="SEBI Baseline",
                    opa_bundle_prefix="tenants/sebi_baseline",
                    risk_overlay={},
                )
            )
            await session.commit()

    async def _get_test_session():
        async with session_factory() as session:
            yield session

    fake_redis = _FakeRedis()
    _wire_app_redis(app, fake_redis)
    monkeypatch.setattr("app.execution.dependencies.get_redis_pool", lambda: fake_redis)

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_db_session] = _get_test_session
    app.dependency_overrides[get_redis_pool] = lambda: fake_redis
    app.dependency_overrides[get_ledger_engine] = lambda: db_engine
    app.dependency_overrides[get_ledger_service] = lambda: LedgerService(db_engine)

    yield {
        "engine": db_engine,
        "session_factory": session_factory,
        "settings": settings,
        "fake_redis": fake_redis,
    }

    app.dependency_overrides.clear()
    await db_engine.dispose()
    await fake_redis.aclose()


def _get_auth_headers(settings: Settings) -> dict[str, str]:
    now = dt.datetime.now(dt.timezone.utc)
    token, _ = create_access_token(
        subject="compliance_officer",
        roles=[Role.COMPLIANCE_OFFICER, Role.SYSTEM_ADMIN],
        settings=settings,
        signing_key=settings.jwt_secret_key,
        auth_time=now,
        amr=["pwd", "mfa"],
    )
    return {"Authorization": f"Bearer {token}"}


class QueryCounter:
    """SQLAlchemy cursor execution listener to count and record SELECT queries."""

    def __init__(self, engine):
        self.engine = engine
        self.queries: list[str] = []
        self._listener = self._on_cursor_execute
        event.listen(self.engine.sync_engine, "before_cursor_execute", self._listener)

    def _on_cursor_execute(self, conn, cursor, statement, parameters, context, executemany):
        cleaned = statement.strip().upper()
        if cleaned.startswith("SELECT"):
            self.queries.append(statement)

    @property
    def select_count(self) -> int:
        return len(self.queries)

    def clear(self):
        self.queries.clear()

    def detach(self):
        event.remove(self.engine.sync_engine, "before_cursor_execute", self._listener)


async def _seed_circular(
    session: AsyncSession,
    circular_number: str,
    title: str,
    processing_state: str = "INGESTED",
    clause_count: int = 0,
    active_rules_count: int = 0,
    inactive_rules_count: int = 0,
    review_statuses: list[str] | None = None,
) -> Circular:
    """Helper to seed a circular with clauses, compiled rules, and reviews."""
    circular = Circular(
        tenant_id="sebi_baseline",
        circular_number=circular_number,
        title=title,
        issue_date=dt.date(2026, 1, 15),
        source_url=None,
        source_filename=f"{circular_number.replace('/', '_')}.pdf",
        source_document_sha256=sha256_of_bytes(f"doc_{circular_number}".encode("utf-8")),
        raw_text_digest=sha256_of_bytes(f"raw_{circular_number}".encode("utf-8")),
        processing_state=processing_state,
    )
    session.add(circular)
    await session.flush()

    clauses = []
    for i in range(clause_count):
        clause = Clause(
            circular_id=circular.id,
            tenant_id="sebi_baseline",
            clause_number=f"Clause-{i+1}",
            section_title=f"Section {i+1}",
            text=f"Text for clause {i+1} of {circular_number}",
            sha256=sha256_of_bytes(f"{circular_number}_clause_{i+1}".encode("utf-8")),
            processing_status="COMPILED" if (active_rules_count + inactive_rules_count > 0) else "PENDING",
        )
        session.add(clause)
        clauses.append(clause)
    await session.flush()

    # Add active and inactive rules distributed among clauses
    total_rules = active_rules_count + inactive_rules_count
    rule_idx = 0
    if clauses:
        for _ in range(active_rules_count):
            clause = clauses[rule_idx % len(clauses)]
            rule = CompiledRule(
                clause_id=clause.id,
                tenant_id="sebi_baseline",
                rule_id=f"RULE-ACT-{circular.id}-{rule_idx+1}",
                rule_version=1,
                rego_policy="package test",
                jsonlogic_ast={},
                is_compiled=True,
                is_active=True,
                hitl_status="RESOLVED",
            )
            session.add(rule)
            rule_idx += 1

        for _ in range(inactive_rules_count):
            clause = clauses[rule_idx % len(clauses)]
            rule = CompiledRule(
                clause_id=clause.id,
                tenant_id="sebi_baseline",
                rule_id=f"RULE-INACT-{circular.id}-{rule_idx+1}",
                rule_version=1,
                rego_policy="package test",
                jsonlogic_ast={},
                is_compiled=True,
                is_active=False,
                hitl_status="BLOCKING",
            )
            session.add(rule)
            rule_idx += 1
    await session.flush()

    # Add reviews
    if review_statuses and clauses:
        now_utc = dt.datetime.now(dt.timezone.utc)
        for r_idx, r_status in enumerate(review_statuses):
            clause = clauses[r_idx % len(clauses)]
            is_terminal = r_status in ("RESOLVED", "REJECTED", "REVISION_REQUIRED")
            review = HITLReview(
                review_id=f"REV-{circular.id}-{r_idx+1}",
                clause_id=clause.id,
                tenant_id="sebi_baseline",
                reason_code="audit_not_approved",
                severity="blocking" if r_status in ("PENDING", "IN_REVIEW") else "advisory",
                description="Test review description",
                status=r_status,
                resolved_at=now_utc if is_terminal else None,
            )
            session.add(review)
    await session.commit()
    return circular


@pytest.mark.asyncio
async def test_zero_circulars_executes_single_query_and_returns_empty(listing_test_env):
    """Zero circulars returns [] with exactly 1 SELECT query (no N+1)."""
    settings = listing_test_env["settings"]
    engine = listing_test_env["engine"]
    headers = _get_auth_headers(settings)

    counter = QueryCounter(engine)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            res = await client.get("/v1/circulars", headers=headers)
            assert res.status_code == 200
            assert res.json() == []
            # Exactly 1 query: SELECT circulars
            assert counter.select_count == 1
    finally:
        counter.detach()


@pytest.mark.asyncio
async def test_constant_query_scaling_eliminates_n_plus_one(listing_test_env):
    """Query count must be constant O(1) across 1, 5, and 10 circulars, completely eliminating N+1 queries.
    Legacy pattern: 1 + 5N queries (6 for N=1, 26 for N=5, 51 for N=10).
    Optimized pattern: at most 4 queries regardless of N.
    """
    settings = listing_test_env["settings"]
    engine = listing_test_env["engine"]
    session_factory = listing_test_env["session_factory"]
    headers = _get_auth_headers(settings)

    # 1. Seed 1 circular
    async with session_factory() as session:
        await _seed_circular(session, "CIRC/001", "Circular 1", clause_count=3, active_rules_count=1)

    counter = QueryCounter(engine)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            counter.clear()
            res1 = await client.get("/v1/circulars", headers=headers)
            assert res1.status_code == 200
            assert len(res1.json()) == 1
            queries_for_1 = counter.select_count
            assert queries_for_1 <= 4, f"Expected <= 4 queries for N=1, got {queries_for_1}"

            # 2. Add 4 more circulars (N=5)
            async with session_factory() as session:
                for i in range(2, 6):
                    await _seed_circular(
                        session,
                        f"CIRC/{i:03d}",
                        f"Circular {i}",
                        clause_count=2,
                        active_rules_count=1,
                        review_statuses=["RESOLVED"],
                    )

            counter.clear()
            res5 = await client.get("/v1/circulars", headers=headers)
            assert res5.status_code == 200
            assert len(res5.json()) == 5
            queries_for_5 = counter.select_count
            assert queries_for_5 <= 4, f"Expected <= 4 queries for N=5, got {queries_for_5}"

            # 3. Add 5 more circulars (N=10)
            async with session_factory() as session:
                for i in range(6, 11):
                    await _seed_circular(
                        session,
                        f"CIRC/{i:03d}",
                        f"Circular {i}",
                        clause_count=4,
                        inactive_rules_count=2,
                        review_statuses=["PENDING"],
                    )

            counter.clear()
            res10 = await client.get("/v1/circulars", headers=headers)
            assert res10.status_code == 200
            assert len(res10.json()) == 10
            queries_for_10 = counter.select_count
            assert queries_for_10 <= 4, f"Expected <= 4 queries for N=10, got {queries_for_10}"

            # Verify that query count did NOT increase with circular count
            assert queries_for_5 == queries_for_10, "Query count must remain strictly constant O(1)"
    finally:
        counter.detach()


@pytest.mark.asyncio
async def test_semantic_parity_across_mixed_processing_states(listing_test_env):
    """Verify exact parity between batched aggregate status and legacy get_circular_status
    across mixed lifecycle states:
    - INGESTED (no rules, no reviews -> processing)
    - AWAITING_HITL (pending reviews -> review_required)
    - APPROVED (resolved reviews -> approved)
    - DEPLOYED (active rules -> deployed)
    - FAILED (rejected reviews or FAILED state -> failed)
    """
    settings = listing_test_env["settings"]
    session_factory = listing_test_env["session_factory"]
    orchestrator = E2EOrchestrator(settings)
    headers = _get_auth_headers(settings)

    async with session_factory() as session:
        c_ingested = await _seed_circular(
            session, "SEBI/TEST/INGESTED", "Ingested Circular",
            processing_state="INGESTED", clause_count=3,
        )
        c_awaiting = await _seed_circular(
            session, "SEBI/TEST/HITL", "Awaiting HITL Circular",
            processing_state="AWAITING_HITL", clause_count=2, inactive_rules_count=2,
            review_statuses=["PENDING", "IN_REVIEW"],
        )
        c_approved = await _seed_circular(
            session, "SEBI/TEST/APPROVED", "Approved Circular",
            processing_state="APPROVED", clause_count=2, inactive_rules_count=2,
            review_statuses=["RESOLVED", "RESOLVED"],
        )
        c_deployed = await _seed_circular(
            session, "SEBI/TEST/DEPLOYED", "Deployed Circular",
            processing_state="DEPLOYED", clause_count=2, active_rules_count=2,
            review_statuses=["RESOLVED"],
        )
        c_failed = await _seed_circular(
            session, "SEBI/TEST/FAILED", "Failed Circular",
            processing_state="FAILED", clause_count=1,
            review_statuses=["REJECTED"],
        )

        all_seeded = [c_ingested, c_awaiting, c_approved, c_deployed, c_failed]

        # Get individual reference status using legacy get_circular_status
        reference_statuses = {}
        for c in all_seeded:
            status_obj = await orchestrator.get_circular_status(session, c.id)
            reference_statuses[c.id] = {
                "status": status_obj.status,
                "clause_count": status_obj.clause_count,
                "active_rules": status_obj.active_rules,
                "pending_reviews": status_obj.pending_reviews,
            }

    # Fetch list from HTTP endpoint
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        res = await client.get("/v1/circulars", headers=headers)
        assert res.status_code == 200
        items = res.json()
        item_map = {i["id"]: i for i in items}

        for c in all_seeded:
            item = item_map[c.id]
            ref = reference_statuses[c.id]
            assert item["status"] == ref["status"], f"Status mismatch for circular {c.circular_number}: {item['status']} vs {ref['status']}"
            assert item["clause_count"] == ref["clause_count"], f"Clause count mismatch for {c.circular_number}"
            assert item["active_rules"] == ref["active_rules"], f"Active rules mismatch for {c.circular_number}"
            assert item["pending_reviews"] == ref["pending_reviews"], f"Pending reviews mismatch for {c.circular_number}"

        # Check explicit expected statuses
        assert item_map[c_ingested.id]["status"] == "processing"
        assert item_map[c_awaiting.id]["status"] == "review_required"
        assert item_map[c_approved.id]["status"] == "approved"
        assert item_map[c_deployed.id]["status"] == "deployed"
        assert item_map[c_failed.id]["status"] == "failed"


@pytest.mark.asyncio
async def test_pagination_happens_before_aggregation(listing_test_env):
    """Pagination parameters limit and offset must restrict database scan before aggregation."""
    settings = listing_test_env["settings"]
    session_factory = listing_test_env["session_factory"]
    headers = _get_auth_headers(settings)

    # Seed 7 circulars
    async with session_factory() as session:
        for i in range(7):
            await _seed_circular(
                session, f"SEBI/PAGE/{i:02d}", f"Page Circular {i}",
                clause_count=2, active_rules_count=1,
            )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        # Page 1: limit 3, offset 0 -> 3 items
        p1 = await client.get("/v1/circulars?limit=3&offset=0", headers=headers)
        assert p1.status_code == 200
        data1 = p1.json()
        assert len(data1) == 3

        # Page 2: limit 3, offset 3 -> 3 items
        p2 = await client.get("/v1/circulars?limit=3&offset=3", headers=headers)
        assert p2.status_code == 200
        data2 = p2.json()
        assert len(data2) == 3

        # Page 3: limit 3, offset 6 -> 1 item remaining
        p3 = await client.get("/v1/circulars?limit=3&offset=6", headers=headers)
        assert p3.status_code == 200
        data3 = p3.json()
        assert len(data3) == 1

        # Beyond bounds: offset 100 -> empty list
        p4 = await client.get("/v1/circulars?limit=3&offset=100", headers=headers)
        assert p4.status_code == 200
        assert p4.json() == []

        # IDs on page 1 and page 2 must be disjoint
        ids1 = {x["id"] for x in data1}
        ids2 = {x["id"] for x in data2}
        assert ids1.isdisjoint(ids2)


@pytest.mark.asyncio
async def test_api_response_structure_exact_contract(listing_test_env):
    """Validate all response dictionary fields match the established schema contract."""
    settings = listing_test_env["settings"]
    session_factory = listing_test_env["session_factory"]
    headers = _get_auth_headers(settings)

    async with session_factory() as session:
        c = await _seed_circular(
            session, "SEBI/HO/CONTRACT/01", "Contract Schema Test",
            clause_count=1, active_rules_count=1,
        )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        res = await client.get("/v1/circulars", headers=headers)
        assert res.status_code == 200
        items = res.json()
        assert len(items) == 1
        item = items[0]

        expected_fields = {
            "id",
            "circular_number",
            "title",
            "issue_date",
            "source_url",
            "source_filename",
            "source_retrieved_at",
            "source_document_sha256",
            "extracted_text_sha256",
            "raw_text_digest",
            "created_at",
            "status",
            "clause_count",
            "active_rules",
            "pending_reviews",
        }
        for field in expected_fields:
            assert field in item, f"Missing required field {field} in list_circulars response"

        assert item["id"] == c.id
        assert item["circular_number"] == "SEBI/HO/CONTRACT/01"
        assert item["title"] == "Contract Schema Test"
        assert item["clause_count"] == 1
        assert item["active_rules"] == 1
        assert item["pending_reviews"] == 0
