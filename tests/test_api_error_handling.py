"""Tests for hardened API error handling in RegEngine.

Verifies:
1. Internal 500 exceptions return stable generic messages without leaking internal details,
   stack traces, SQL errors, or filesystem paths to clients.
2. Server-side logs record the full exception and traceback along with correlation/request ID.
3. Correlation IDs (X-Request-ID / X-Correlation-ID) are propagated from requests or generated
   and attached to all responses, including error responses.
4. Typed pipeline exceptions (ExtractionBackendError, EmbeddingError, IndexingError, etc.)
   return sanitized, stable HTTP messages.
5. Ingestion, SAML, and Analytics errors do not expose paths or internal configs.
6. Legitimate 4xx user validation, authentication, and authorization errors remain intact.
"""
from __future__ import annotations

import logging
import sys
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import Settings, get_settings
from app.db.base import Base
from app.db.session import get_db_session
from app.db.tenant_session import get_admin_db_session
from app.execution.dependencies import get_redis_pool
from app.main import app
from app.models import ClauseChunk
from app.parsing.exceptions import (
    ChunkingError,
    EmbeddingError,
    ExtractionBackendError,
    IndexingError,
    ParseTimeoutError,
    ScannedDocumentError,
    UnsupportedFileError,
)
from app.security.jwt import create_access_token
from app.security.models import Role
from app.storage.object_store import InvalidFileTypeError, PathTraversalError


class _FakePipeline:
    def __init__(self, fake_redis: _FakeRedis) -> None:
        self.fake_redis = fake_redis
        self.ops: list[tuple[str, any]] = []

    def incr(self, key: str) -> _FakePipeline:
        self.ops.append(("incr", key))
        return self

    def expire(self, key: str, seconds: int, nx: bool = False) -> _FakePipeline:
        self.ops.append(("expire", key, seconds))
        return self

    async def execute(self) -> list[any]:
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
        self.store: dict[str, any] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.sets: dict[str, set[str]] = {}
        self.lists: dict[str, list[str]] = {}

    def pipeline(self) -> _FakePipeline:
        return _FakePipeline(self)

    async def get(self, key: str) -> any:
        return self.store.get(key)

    async def set(self, key: str, value: any, ex: int | None = None) -> bool:
        self.store[key] = value
        return True

    async def ttl(self, key: str) -> int:
        return 60

    async def exists(self, *keys: str) -> int:
        return sum(1 for k in keys if k in self.store)

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
async def error_test_env():
    settings = Settings(
        environment="development",
        database_url="sqlite+aiosqlite:///:memory:",
        ledger_database_url="sqlite+aiosqlite:///:memory:",
        jwt_secret_key="test-secret-key-at-least-32-chars-long-for-jwt-signing",
        saml_enabled=True,
    )

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
    app.dependency_overrides[get_redis_pool] = lambda: fake_redis
    app.dependency_overrides[get_db_session] = _get_test_session
    app.dependency_overrides[get_admin_db_session] = _get_test_session

    token, _ = create_access_token(
        subject="admin@regengine.ai",
        roles=[Role.SYSTEM_ADMIN, Role.COMPLIANCE_OFFICER],
        settings=settings,
        signing_key=settings.jwt_secret_key,
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, headers, settings

    app.dependency_overrides.clear()
    await db_engine.dispose()


@pytest.mark.asyncio
async def test_correlation_id_headers_attached_to_responses(error_test_env):
    """Test that X-Request-ID and X-Correlation-ID are always attached,
    propagating the caller's request ID or generating a fresh UUID."""
    client, headers, _ = error_test_env

    # 1. Custom incoming request ID
    custom_id = "req-custom-trace-98765"
    req_headers = {**headers, "X-Request-ID": custom_id}
    res = await client.get("/healthz", headers=req_headers)
    assert res.status_code == 200
    assert res.headers.get("X-Request-ID") == custom_id
    assert res.headers.get("X-Correlation-ID") == custom_id

    # 2. Generated UUID when no incoming ID
    res2 = await client.get("/healthz", headers=headers)
    assert res2.status_code == 200
    gen_id = res2.headers.get("X-Request-ID")
    assert gen_id is not None
    # Validate it is a valid UUID
    uuid.UUID(gen_id)
    assert res2.headers.get("X-Correlation-ID") == gen_id


@pytest.mark.asyncio
async def test_unhandled_exception_returns_500_with_safe_generic_detail(error_test_env, caplog):
    """Test that unexpected server crashes (e.g. database errors) return HTTP 500
    with 'Internal server error.' and do not expose SQL, stack traces, or credentials."""
    client, headers, _ = error_test_env

    # Simulate an unexpected database crash with SQL and internal host info
    secret_sql_error = OperationalError(
        statement="SELECT password_hash, api_secret FROM tenants WHERE id = 'secret-tenant'",
        params={},
        orig=Exception("connection to server at '10.240.0.12', port 5432 failed: password authentication failed"),
    )

    with patch("app.services.orchestrator.E2EOrchestrator.get_circular_status", side_effect=secret_sql_error), caplog.at_level(logging.ERROR):
        res = await client.get("/v1/circulars/999/status", headers=headers)

    assert res.status_code == 500
    data = res.json()
    assert data["detail"] == "Internal server error."

    # Verify sensitive details were NEVER sent to client
    body_text = res.text
    assert "password_hash" not in body_text
    assert "api_secret" not in body_text
    assert "10.240.0.12" not in body_text
    assert "SELECT" not in body_text
    assert "OperationalError" not in body_text
    assert "Traceback" not in body_text

    # Verify correlation ID is present in headers
    assert "X-Request-ID" in res.headers
    req_id = res.headers["X-Request-ID"]

    # Verify server-side logs DO contain the details and correlation ID
    assert req_id in caplog.text
    assert "Unhandled internal exception" in caplog.text


@pytest.mark.asyncio
async def test_parsing_pipeline_backend_error_does_not_leak_paths(error_test_env, caplog):
    """Test that ExtractionBackendError does not expose internal filesystem paths or command lines."""
    client, headers, _ = error_test_env

    sensitive_backend_error = ExtractionBackendError(
        "All extraction backends failed (pypdf, pdfplumber). "
        "pypdf=FileNotFoundError('/var/data/internal/sensitive_circular.pdf') "
        "pdfplumber=CalledProcessError(cmd=['/usr/bin/python', '/opt/regengine/scripts/extract.py'])"
    )

    files = {"file": ("circular.pdf", b"%PDF-1.4 test dummy", "application/pdf")}
    auth_headers = {"Authorization": headers["Authorization"]}

    with patch("app.api.routes.parse_pdf_bytes", side_effect=sensitive_backend_error), caplog.at_level(logging.WARNING):
        res = await client.post("/v1/circulars/parse", headers=auth_headers, files=files)

    assert res.status_code == 502
    data = res.json()
    assert data["detail"] == "Document text extraction failed across all configured backends."

    # Verify internal paths, commands, and backend names are not leaked
    assert "/var/data/internal" not in res.text
    assert "/usr/bin/python" not in res.text
    assert "/opt/regengine" not in res.text
    assert "FileNotFoundError" not in res.text

    # Verify correlation headers
    assert "X-Request-ID" in res.headers


@pytest.mark.asyncio
async def test_indexing_error_does_not_leak_qdrant_connection_details(error_test_env):
    """Test that IndexingError and EmbeddingError return safe generic messages without host/key leaks."""
    client, headers, _ = error_test_env

    chunk_data = {
        "chunk_id": "chunk-1",
        "clause_number": "1.1",
        "section_title": "Scope",
        "section_path": ["1.1"],
        "text": "test clause content",
        "sha256": "abc123456",
    }

    # 1. Embedding Error
    sensitive_embedding_error = EmbeddingError(
        "Failed to embed 5 chunk(s): APIConnectionError(url='https://api.openai.com/v1', api_key='sk-prod-secret-99999')"
    )
    with patch("app.api.routes.index_chunks", side_effect=sensitive_embedding_error):
        res = await client.post(
            "/v1/circulars/index",
            headers=headers,
            json={"chunks": [chunk_data]},
        )
    assert res.status_code == 502
    assert res.json()["detail"] == "Failed to generate vector embeddings for document chunks."
    assert "sk-prod-secret-99999" not in res.text
    assert "api.openai.com" not in res.text

    # 2. Indexing Error
    sensitive_indexing_error = IndexingError(
        "Failed to ensure Qdrant collection 'circular_clauses': ConnectError('http://qdrant.internal.regengine:6333')"
    )
    with patch("app.api.routes.index_chunks", side_effect=sensitive_indexing_error):
        res2 = await client.post(
            "/v1/circulars/index",
            headers=headers,
            json={"chunks": [chunk_data]},
        )
    assert res2.status_code == 502
    assert res2.json()["detail"] == "Failed to index document chunks into vector search."
    assert "qdrant.internal.regengine" not in res2.text


@pytest.mark.asyncio
async def test_process_e2e_unhandled_error_does_not_leak_exception(error_test_env, caplog):
    """Test that /v1/circulars/process-e2e returns a safe generic 500 when an unhandled exception occurs."""
    client, headers, _ = error_test_env

    files = {"file": ("test.pdf", b"%PDF-1.4 test", "application/pdf")}
    auth_headers = {"Authorization": headers["Authorization"]}

    internal_err = RuntimeError("Database connection string: postgresql://admin:SuperSecretPass123@db:5432/regengine")

    with patch("app.services.orchestrator.E2EOrchestrator.process_circular_pdf", side_effect=internal_err), caplog.at_level(logging.ERROR):
        res = await client.post("/v1/circulars/process-e2e", headers=auth_headers, files=files)

    assert res.status_code == 500
    assert res.json()["detail"] == "Internal error processing circular."
    assert "SuperSecretPass123" not in res.text
    assert "postgresql://" not in res.text
    assert "RuntimeError" not in res.text


@pytest.mark.asyncio
async def test_storage_upload_path_traversal_does_not_leak_storage_paths(error_test_env):
    """Test that storage upload path traversal errors do not expose root directory paths."""
    client, headers, _ = error_test_env

    files = {"file": ("test.pdf", b"%PDF-1.4 test", "application/pdf")}
    auth_headers = {"Authorization": headers["Authorization"]}

    traversal_err = PathTraversalError("Path escapes storage root directory: /srv/app/storage/uploads/../../etc/passwd")

    with patch("app.storage.object_store.upload_bytes", side_effect=traversal_err):
        res = await client.post("/v1/ingestion/uploads", headers=auth_headers, files=files)

    assert res.status_code == 422
    assert res.json()["detail"] == "Upload rejected: Invalid filename or storage key."
    assert "/srv/app/storage" not in res.text
    assert "etc/passwd" not in res.text


@pytest.mark.asyncio
async def test_saml_sp_metadata_does_not_leak_configuration_errors(error_test_env, caplog):
    """Test that SAML SP metadata generation failure returns a clean 500 without leaking config dicts or cert paths."""
    client, headers, _ = error_test_env

    sp_errors = ["sp_cert_not_found at /etc/ssl/private/saml_sp.key", "invalid_entity_id: https://internal.idp.local"]

    mock_saml_settings = MagicMock()
    mock_saml_settings.check_sp_settings.return_value = sp_errors
    mock_settings_cls = MagicMock(return_value=mock_saml_settings)

    mock_mod = ModuleType("onelogin.saml2.settings")
    mock_mod.OneLogin_Saml2_Settings = mock_settings_cls

    with patch.dict(sys.modules, {
        "onelogin": ModuleType("onelogin"),
        "onelogin.saml2": ModuleType("onelogin.saml2"),
        "onelogin.saml2.settings": mock_mod,
    }), caplog.at_level(logging.ERROR):
        res = await client.get("/v1/auth/saml/metadata", headers=headers)

    assert res.status_code == 500
    assert res.json()["detail"] == "Invalid SAML service provider configuration."
    assert "/etc/ssl/private" not in res.text
    assert "internal.idp.local" not in res.text
    assert "Invalid SAML SP configuration" in caplog.text


@pytest.mark.asyncio
async def test_date_parsing_validation_error_is_clean_and_safe(error_test_env):
    """Test that date formatting validation returns clean, safe 422 responses without raw exception dumps."""
    client, headers, _ = error_test_env

    # 1. LLM cost date parsing with an invalid calendar date matching YYYY-MM-DD pattern
    res1 = await client.get("/v1/llm-cost/summary?date_from=2026-02-31&date_to=2026-03-01", headers=headers)
    assert res1.status_code == 422
    assert res1.json()["detail"] == "Invalid date format. Expected YYYY-MM-DD."
    assert "Traceback" not in res1.text

    # 2. Analytics summary date parsing with an invalid calendar date
    res2 = await client.get("/v1/analytics/summary?date_from=2026-02-31&date_to=2026-03-01", headers=headers)
    assert res2.status_code == 422
    assert res2.json()["detail"] == "Invalid date format. Expected YYYY-MM-DD."
    assert "Traceback" not in res2.text


@pytest.mark.asyncio
async def test_analytics_pipeline_error_does_not_leak_database_details(error_test_env, caplog):
    """Test that analytics aggregation pipeline failure returns safe 500 message."""
    client, headers, _ = error_test_env

    crash = RuntimeError("Ledger integrity failure: HMAC signature mismatch for block 4982 in table ledger_entries")

    with patch("app.analytics.aggregator.ComplianceAggregator.build_aggregated_report", side_effect=crash), caplog.at_level(logging.ERROR):
        res = await client.get("/v1/analytics/summary?date_from=2026-01-01&date_to=2026-01-10", headers=headers)

    assert res.status_code == 500
    assert res.json()["detail"] == "Analytics pipeline failed to generate report."
    assert "HMAC signature mismatch" not in res.text
    assert "ledger_entries" not in res.text


@pytest.mark.asyncio
async def test_top_level_parsing_error_handler_returns_safe_response(error_test_env, caplog):
    """Test that any unhandled ParsingError that bubbles up to FastAPI top-level handler
    returns a generic 500 with correlation ID."""
    client, headers, _ = error_test_env

    # Simulate an unhandled ParsingError bubbling up from deep in the stack
    unhandled_parsing_err = ChunkingError("Internal chunking failed: recursive regex depth exceeded on /tmp/doc_clause.txt")

    with patch("app.api.routes.parse_pdf_bytes", side_effect=unhandled_parsing_err), caplog.at_level(logging.ERROR):
        # We temporarily clear _map_status match to simulate an unmapped ParsingError reaching top level
        with patch.dict("app.api.routes._ERROR_STATUS_MAP", {}, clear=True):
            files = {"file": ("circular.pdf", b"%PDF-1.4 test", "application/pdf")}
            auth_headers = {"Authorization": headers["Authorization"]}
            res = await client.post("/v1/circulars/parse", headers=auth_headers, files=files)

    assert res.status_code == 500
    assert res.json()["detail"] == "Internal error while parsing the document."
    assert "/tmp/doc_clause.txt" not in res.text
    assert "X-Request-ID" in res.headers


@pytest.mark.asyncio
async def test_legitimate_4xx_errors_preserved(error_test_env):
    """Test that legitimate 4xx user validation, authentication, and authorization errors
    are NOT hidden and return proper status codes and messages."""
    client, headers, _ = error_test_env

    # 1. 401 Unauthorized for missing auth
    res_401 = await client.get("/v1/circulars")
    assert res_401.status_code == 401
    assert "X-Request-ID" in res_401.headers

    # 2. 404 Not Found for non-existent circular
    res_404 = await client.get("/v1/circulars/88888/status", headers=headers)
    assert res_404.status_code == 404
    assert res_404.json()["detail"] == "Circular with ID 88888 not found."

    # 3. 415 Unsupported Media Type
    files = {"file": ("circular.txt", b"plain text", "text/plain")}
    auth_headers = {"Authorization": headers["Authorization"]}
    res_415 = await client.post("/v1/circulars/parse", headers=auth_headers, files=files)
    assert res_415.status_code == 415
    assert "Unsupported content type" in res_415.json()["detail"]

    # 4. 422 Unprocessable Entity
    res_422 = await client.get("/v1/llm-cost/summary?date_from=2026-05-10&date_to=2026-05-01", headers=headers)
    assert res_422.status_code == 422
    assert "date_from must be on or before date_to" in res_422.json()["detail"]
