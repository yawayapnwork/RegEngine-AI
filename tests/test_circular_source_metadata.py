"""Tests for regulatory circular source metadata model:
1. URL ingestion: source_url preserved, source_filename preserved, source_retrieved_at preserved.
2. Local file upload: source_url is strictly NULL (None), source_filename preserved.
3. Missing URL: source_url is NULL (None), never a fake URL.
4. Filename preservation: actual filename stored in source_filename, never in source_url.
5. API endpoints: /v1/circulars, /v1/circulars/process-e2e, details, status.
6. Audit binder source PDF resolution via source_filename.
7. Alembic migration backfill verification.
"""
from __future__ import annotations

import datetime as dt
from io import BytesIO
from pathlib import Path
from typing import Any
import httpx
import pytest
import pytest_asyncio
from reportlab.pdfgen import canvas
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import Settings, get_settings
from app.db.base import Base
from app.db.models import Circular, Tenant
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
from app.ledger.dependencies import get_ledger_engine, get_ledger_service
from app.ledger.service import LedgerService
from app.main import app
from app.models import CircularMetadata, ParseResult
from app.parsing.hashing import sha256_of_bytes, sha256_of_extracted_text
from app.reporting.audit_binder import _find_source_pdf
from app.security.jwt import create_access_token
from app.security.models import Role
from app.services.orchestrator import E2EOrchestrator


def _create_test_pdf(title: str, circular_number: str) -> bytes:
    buf = BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(100, 750, f"Circular No. {circular_number}")
    c.drawString(100, 730, "15 January 2026")
    c.drawString(100, 700, f"Title: {title}")
    c.drawString(100, 670, "1. Scope and Applicability")
    c.drawString(100, 650, "1.1 Registered brokers must comply with margin regulations.")
    c.drawString(100, 620, "2. Margin Requirement")
    c.drawString(100, 600, "2.1 Maintain upfront margin of not less than 25%.")
    c.save()
    return buf.getvalue()


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
async def metadata_test_env(tmp_path: Path):
    settings = Settings(
        environment="development",
        database_url="sqlite+aiosqlite:///:memory:",
        ledger_database_url="sqlite+aiosqlite:///:memory:",
        jwt_secret_key="test_secret_for_source_metadata_testing_min_32_chars!",
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

    async def _get_test_session():
        async with session_factory() as session:
            yield session

    fake_redis = _FakeRedis()
    _wire_app_redis(app, fake_redis)

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_db_session] = _get_test_session
    app.dependency_overrides[get_redis_pool] = lambda: fake_redis
    app.dependency_overrides[get_ledger_engine] = lambda: db_engine
    app.dependency_overrides[get_ledger_service] = lambda: LedgerService(db_engine)

    yield {
        "engine": db_engine,
        "session_factory": session_factory,
        "settings": settings,
        "tmp_path": tmp_path,
        "fake_redis": fake_redis,
    }

    app.dependency_overrides.clear()
    await db_engine.dispose()
    await fake_redis.aclose()


@pytest.mark.asyncio
async def test_local_file_upload_has_null_source_url_and_preserves_filename(metadata_test_env):
    """Local file upload: source_url must be NULL, actual filename preserved in source_filename."""
    session_factory = metadata_test_env["session_factory"]
    settings = metadata_test_env["settings"]
    orchestrator = E2EOrchestrator(settings)

    pdf_bytes = _create_test_pdf("Local Upload Test", "SEBI/HO/MRD/2026/001")
    pdf_sha256 = sha256_of_bytes(pdf_bytes)

    async with session_factory() as session:
        result = await orchestrator.process_circular_pdf(
            session=session,
            file_bytes=pdf_bytes,
            filename="my_uploaded_circular_2026.pdf",
            source_url=None,  # local upload, no external URL
        )

        assert result.source_url is None
        assert result.source_filename == "my_uploaded_circular_2026.pdf"
        assert result.source_document_sha256 == pdf_sha256

        # Verify database record
        circular = await session.get(Circular, result.circular_id)
        assert circular is not None
        assert circular.source_url is None, "source_url must be NULL for local uploads"
        assert circular.source_filename == "my_uploaded_circular_2026.pdf"
        assert circular.source_document_sha256 == pdf_sha256
        assert circular.raw_text_digest == result.extracted_text_sha256


@pytest.mark.asyncio
async def test_url_ingestion_preserves_url_and_filename(metadata_test_env):
    """URL ingestion: source_url and source_filename are both cleanly populated."""
    session_factory = metadata_test_env["session_factory"]
    settings = metadata_test_env["settings"]
    orchestrator = E2EOrchestrator(settings)

    pdf_bytes = _create_test_pdf("URL Ingestion Test", "SEBI/HO/MIRSD/2026/045")
    external_url = "https://www.sebi.gov.in/legal/circulars/jan-2026/margin_trading_2026.pdf"
    retrieved_time = dt.datetime(2026, 1, 15, 10, 30, tzinfo=dt.timezone.utc)

    async with session_factory() as session:
        result = await orchestrator.process_circular_pdf(
            session=session,
            file_bytes=pdf_bytes,
            filename="margin_trading_2026.pdf",
            source_url=external_url,
            source_retrieved_at=retrieved_time,
        )

        assert result.source_url == external_url
        assert result.source_filename == "margin_trading_2026.pdf"
        assert result.source_retrieved_at == retrieved_time

        # Verify database record
        circular = await session.get(Circular, result.circular_id)
        assert circular is not None
        assert circular.source_url == external_url
        assert circular.source_filename == "margin_trading_2026.pdf"
        assert circular.source_retrieved_at is not None
        stored_utc = (
            circular.source_retrieved_at.replace(tzinfo=dt.timezone.utc)
            if circular.source_retrieved_at.tzinfo is None
            else circular.source_retrieved_at
        )
        assert stored_utc == retrieved_time


@pytest.mark.asyncio
async def test_missing_url_and_missing_filename(metadata_test_env):
    """When both URL and filename are omitted, source_url is NULL, never a fake URL."""
    session_factory = metadata_test_env["session_factory"]
    settings = metadata_test_env["settings"]
    orchestrator = E2EOrchestrator(settings)

    pdf_bytes = _create_test_pdf("No Metadata Test", "SEBI/HO/MRD/2026/099")

    async with session_factory() as session:
        result = await orchestrator.process_circular_pdf(
            session=session,
            file_bytes=pdf_bytes,
            filename=None,
            source_url=None,
        )

        assert result.source_url is None
        assert result.source_filename is None

        circular = await session.get(Circular, result.circular_id)
        assert circular is not None
        assert circular.source_url is None
        assert circular.source_filename is None


@pytest.mark.asyncio
async def test_never_puts_filename_into_source_url(metadata_test_env):
    """Strict validation prevents passing a filename/local path into source_url."""
    session_factory = metadata_test_env["session_factory"]
    settings = metadata_test_env["settings"]
    orchestrator = E2EOrchestrator(settings)

    pdf_bytes = _create_test_pdf("Fake URL Guard Test", "SEBI/HO/MRD/2026/077")

    async with session_factory() as session:
        # Caller erroneously attempts to pass a filename in source_url
        result = await orchestrator.process_circular_pdf(
            session=session,
            file_bytes=pdf_bytes,
            filename="local_file.pdf",
            source_url="local_file.pdf",  # NOT a valid URL!
        )

        # source_url must be sanitized to None!
        assert result.source_url is None, "source_url must NEVER contain a filename"
        assert result.source_filename == "local_file.pdf"

        circular = await session.get(Circular, result.circular_id)
        assert circular.source_url is None
        assert circular.source_filename == "local_file.pdf"


@pytest.mark.asyncio
async def test_filename_preservation_with_path_traversal(metadata_test_env):
    """Filename path components are safely stripped to the basename and preserved."""
    session_factory = metadata_test_env["session_factory"]
    settings = metadata_test_env["settings"]
    orchestrator = E2EOrchestrator(settings)

    pdf_bytes = _create_test_pdf("Path Traversal Test", "SEBI/HO/MRD/2026/088")

    async with session_factory() as session:
        result = await orchestrator.process_circular_pdf(
            session=session,
            file_bytes=pdf_bytes,
            filename="/var/uploads/secure/SEBI Circular 2026 Final.pdf",
        )

        assert result.source_url is None
        assert result.source_filename == "SEBI Circular 2026 Final.pdf"

        circular = await session.get(Circular, result.circular_id)
        assert circular.source_filename == "SEBI Circular 2026 Final.pdf"
        assert circular.source_url is None


@pytest.mark.asyncio
async def test_api_endpoints_expose_source_metadata(metadata_test_env):
    """API endpoints (/v1/circulars/process-e2e, /v1/circulars, details, status)
    correctly expose separate source_url (None) and source_filename."""
    settings = metadata_test_env["settings"]
    now = dt.datetime.now(dt.timezone.utc)
    token, _ = create_access_token(
        subject="admin_user",
        roles=[Role.COMPLIANCE_OFFICER, Role.SYSTEM_ADMIN],
        settings=settings,
        signing_key=settings.jwt_secret_key,
        auth_time=now,
        amr=["pwd", "mfa"],
    )

    pdf_bytes = _create_test_pdf("API E2E Test", "SEBI/HO/MRD/2026/300")
    uploaded_filename = "sebi_regulatory_test_2026.pdf"

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        # 1. Process E2E
        res = await client.post(
            "/v1/circulars/process-e2e",
            headers={"Authorization": f"Bearer {token}"},
            files={"file": (uploaded_filename, pdf_bytes, "application/pdf")},
        )
        assert res.status_code == 200
        data = res.json()
        circular_id = data["circular_id"]
        assert data["source_url"] is None
        assert data["source_filename"] == uploaded_filename
        assert data["source_document_sha256"] == sha256_of_bytes(pdf_bytes)

        # 2. List circulars
        list_res = await client.get(
            "/v1/circulars",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert list_res.status_code == 200
        items = list_res.json()
        item = next(i for i in items if i["id"] == circular_id)
        assert item["source_url"] is None
        assert item["source_filename"] == uploaded_filename
        assert item["source_document_sha256"] == sha256_of_bytes(pdf_bytes)

        # 3. Get circular details
        details_res = await client.get(
            f"/v1/circulars/{circular_id}/details",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert details_res.status_code == 200
        details_data = details_res.json()
        c_detail = details_data["circular"]
        assert c_detail["source_url"] is None
        assert c_detail["source_filename"] == uploaded_filename
        assert c_detail["source_document_sha256"] == sha256_of_bytes(pdf_bytes)

        # 4. Status endpoint
        status_res = await client.get(
            f"/v1/circulars/{circular_id}/status",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert status_res.status_code == 200
        status_data = status_res.json()
        assert status_data["source_url"] is None
        assert status_data["source_filename"] == uploaded_filename
        assert status_data["source_document_sha256"] == sha256_of_bytes(pdf_bytes)


@pytest.mark.asyncio
async def test_audit_binder_finds_pdf_via_source_filename(metadata_test_env):
    """Audit binder _find_source_pdf locates PDF on disk when source_url is NULL."""
    settings = metadata_test_env["settings"]
    download_dir = Path(settings.ingestion_pdf_download_dir)

    target_filename = "archived_sebi_circular.pdf"
    file_path = download_dir / target_filename
    file_path.write_bytes(b"%PDF-1.4 dummy content")

    # Mock circular with source_filename set and source_url is None
    class _MockCircular:
        circular_number = "SEBI/HO/2026/50"
        source_url = None
        source_filename = target_filename

    found = _find_source_pdf(_MockCircular(), settings)
    assert found is not None
    assert found == file_path
    assert found.name == target_filename


@pytest.mark.asyncio
async def test_migration_backfill_logic(metadata_test_env):
    """Verifies that the backfill SQL logic moves filenames out of source_url."""
    session_factory = metadata_test_env["session_factory"]

    async with session_factory() as session:
        # Insert a circular simulating legacy schema where filename was stored in source_url
        legacy_circular = Circular(
            tenant_id="sebi_baseline",
            circular_number="SEBI/LEGACY/001",
            title="Legacy Circular",
            source_url="legacy_circular_filename.pdf",
            source_filename=None,
            raw_text_digest="a" * 64,
            source_document_sha256="b" * 64,
        )
        session.add(legacy_circular)

        # Insert a circular with a real external URL
        url_circular = Circular(
            tenant_id="sebi_baseline",
            circular_number="SEBI/LEGACY/002",
            title="Real URL Circular",
            source_url="https://www.sebi.gov.in/circulars/real.pdf",
            source_filename=None,
            raw_text_digest="c" * 64,
            source_document_sha256="d" * 64,
        )
        session.add(url_circular)
        await session.commit()

        # Run the backfill update query (from migration 0009)
        await session.execute(
            text(
                "UPDATE circulars "
                "SET source_filename = source_url, source_url = NULL "
                "WHERE source_url IS NOT NULL "
                "AND source_url NOT LIKE 'http://%' "
                "AND source_url NOT LIKE 'https://%' "
                "AND source_url NOT LIKE 'ftp://%'"
            )
        )
        await session.commit()
        session.expire_all()

        # Check results
        updated_legacy = (await session.execute(
            select(Circular).where(Circular.circular_number == "SEBI/LEGACY/001")
        )).scalar_one()
        assert updated_legacy.source_url is None, "Legacy filename in source_url must be cleared to NULL"
        assert updated_legacy.source_filename == "legacy_circular_filename.pdf", "Filename must be moved to source_filename"

        updated_url = (await session.execute(
            select(Circular).where(Circular.circular_number == "SEBI/LEGACY/002")
        )).scalar_one()
        assert updated_url.source_url == "https://www.sebi.gov.in/circulars/real.pdf", "Valid external URL must be preserved"
