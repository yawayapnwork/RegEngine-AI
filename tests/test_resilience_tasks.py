"""Tests that each DLQ-integrated Celery task actually does what its
module docstring claims: retry a transient failure with backoff, and
route a permanent-or-exhausted failure to the DLQ (proven with a spy
replacing `route_to_dlq_sync`, so no real Redis is needed).

`task.apply()` runs the task's own function synchronously in-process --
no broker, no worker needed. Important behavioral note this discovered:
for a `bind=True` task, Celery's eager `apply()` does NOT surface
`self.retry()` as a `celery.exceptions.Retry` the caller catches --
`self.request.retries` genuinely increments and the task function is
re-invoked internally, in a loop, with no real sleep between attempts,
until it either succeeds or falls through to this task's own
attempt-budget check and re-raises the original exception. So a "does it
retry" assertion here counts how many times the underlying work function
was actually called, not the exception type that ultimately escapes
`.apply()` -- which is always either the return value or the ORIGINAL
exception, never `Retry` itself.
"""
from __future__ import annotations

import httpx
import pytest

from app.config import Settings
from app.resilience.models import FailureCategory


def _settings(**overrides) -> Settings:
    base = dict(retry_max_attempts_network=1, retry_max_attempts_pipeline=1)
    base.update(overrides)
    return Settings(**base)


# --------------------------------------------------------------------------
# app.compiler.tasks.compile_audited_rule_task
# --------------------------------------------------------------------------


def _valid_audited_rule_dict() -> dict:
    return {
        "rule": {
            "rule_id": "a" * 64 + ":2.1.b",
            "source_chunk_id": "chunk-1",
            "source_sha256": "a" * 64,
            "circular_number": "SEBI/HO/MRD/2024/1",
            "clause_number": "2.1.b",
            "target_entities": [],
            "deterministic_logic": [
                {
                    "metric": "Upfront Margin", "operator": ">=", "value": 20, "unit": "%",
                    "verbatim_evidence": "not less than 20%",
                }
            ],
            "obligation_type": "mandatory",
            "extraction_confidence": 0.95,
        },
        "audit": {
            "rule_id": "a" * 64 + ":2.1.b",
            "verdict": "approved",
            "fidelity_score": 0.98,
            "verified_quote_count": 1,
            "unverified_quote_count": 0,
        },
    }


class TestCompileAuditedRuleTask:
    def test_malformed_ast_routes_to_dlq_and_reraises(self, monkeypatch):
        import app.compiler.tasks as mod
        from app.resilience.exceptions import MalformedASTError

        dlq_calls = []
        monkeypatch.setattr(mod, "route_to_dlq_sync", lambda **kw: dlq_calls.append(kw))

        def _raise(_audited):
            raise MalformedASTError("bad AST")

        monkeypatch.setattr(mod, "compile_audited_rule", _raise)
        audited_dict = _valid_audited_rule_dict()

        with pytest.raises(MalformedASTError):
            mod.compile_audited_rule_task.apply(args=[audited_dict]).get()

        assert len(dlq_calls) == 1
        assert dlq_calls[0]["category"] == FailureCategory.MALFORMED_AST
        assert dlq_calls[0]["task_name"] == "app.compiler.tasks.compile_audited_rule_task"
        assert dlq_calls[0]["payload"] == {"audited_rule_dict": audited_dict}


# --------------------------------------------------------------------------
# app.agents.tasks.extract_and_audit_clause_task
# --------------------------------------------------------------------------


class TestExtractAndAuditClauseTask:
    def _chunk_dict(self) -> dict:
        return {"chunk_id": "c1", "sha256": "a" * 64, "text": "some clause text"}

    def test_transient_failure_is_retried_before_reaching_the_dlq(self, monkeypatch):
        import app.agents.tasks as mod

        monkeypatch.setattr(mod, "get_settings", lambda: _settings(retry_max_attempts_pipeline=4))

        call_count = {"n": 0}

        async def _raise(*args, **kwargs):
            call_count["n"] += 1
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(mod, "extract_and_audit_clause", _raise)
        dlq_calls = []
        monkeypatch.setattr(mod, "route_to_dlq_sync", lambda **kw: dlq_calls.append(kw))

        with pytest.raises(httpx.ConnectError):
            mod.extract_and_audit_clause_task.apply(args=[self._chunk_dict(), None]).get()

        # Retried up to the configured budget (4 attempts total) rather
        # than DLQ-routing on the very first failure -- the actual
        # behavior this task exists to provide for a transient LLM-API
        # error. Only the FINAL exhaustion reaches the DLQ, exactly once.
        assert call_count["n"] == 4
        assert len(dlq_calls) == 1
        assert dlq_calls[0]["category"] == FailureCategory.LLM_EXTRACTION

    def test_non_transient_failure_routes_to_dlq_immediately(self, monkeypatch):
        import app.agents.tasks as mod

        monkeypatch.setattr(mod, "get_settings", lambda: _settings(retry_max_attempts_pipeline=5))

        call_count = {"n": 0}

        async def _raise(*args, **kwargs):
            call_count["n"] += 1
            raise ValueError("the model could not structure this clause")

        monkeypatch.setattr(mod, "extract_and_audit_clause", _raise)
        dlq_calls = []
        monkeypatch.setattr(mod, "route_to_dlq_sync", lambda **kw: dlq_calls.append(kw))

        with pytest.raises(ValueError):
            mod.extract_and_audit_clause_task.apply(args=[self._chunk_dict(), None]).get()

        # NOT retried, despite a generous attempt budget -- is_transient()
        # correctly says a ValueError isn't worth retrying.
        assert call_count["n"] == 1
        assert len(dlq_calls) == 1
        assert dlq_calls[0]["category"] == FailureCategory.LLM_EXTRACTION
        assert dlq_calls[0]["payload"]["chunk_dict"]["chunk_id"] == "c1"

    def test_transient_failure_exhausted_attempts_routes_to_dlq(self, monkeypatch):
        """max_attempts=1 means attempt (1) is not < max_attempts (1) --
        even a transient failure goes straight to the DLQ once the attempt
        budget is spent, rather than retrying forever."""
        import app.agents.tasks as mod

        monkeypatch.setattr(mod, "get_settings", lambda: _settings(retry_max_attempts_pipeline=1))

        async def _raise(*args, **kwargs):
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(mod, "extract_and_audit_clause", _raise)
        dlq_calls = []
        monkeypatch.setattr(mod, "route_to_dlq_sync", lambda **kw: dlq_calls.append(kw))

        with pytest.raises(httpx.ConnectError):
            mod.extract_and_audit_clause_task.apply(args=[self._chunk_dict(), None]).get()

        assert len(dlq_calls) == 1
        assert dlq_calls[0]["category"] == FailureCategory.LLM_EXTRACTION


# --------------------------------------------------------------------------
# app.vectorstore.tasks.index_chunks_task
# --------------------------------------------------------------------------


class TestIndexChunksTask:
    def test_transient_qdrant_failure_is_retried_before_reaching_the_dlq(self, monkeypatch):
        import app.vectorstore.tasks as mod

        monkeypatch.setattr(mod, "get_settings", lambda: _settings(retry_max_attempts_network=4))

        call_count = {"n": 0}

        async def _raise(*args, **kwargs):
            call_count["n"] += 1
            raise ConnectionError("Qdrant unreachable")

        monkeypatch.setattr(mod, "index_chunks", _raise)
        dlq_calls = []
        monkeypatch.setattr(mod, "route_to_dlq_sync", lambda **kw: dlq_calls.append(kw))

        with pytest.raises(ConnectionError):
            mod.index_chunks_task.apply(args=[[{"chunk_id": "c1", "sha256": "a" * 64, "text": "x"}]]).get()

        assert call_count["n"] == 4
        assert len(dlq_calls) == 1

    def test_permanent_embedding_failure_routes_to_vector_ingestion_category(self, monkeypatch):
        import app.vectorstore.tasks as mod

        monkeypatch.setattr(mod, "get_settings", lambda: _settings())

        async def _raise(*args, **kwargs):
            raise ValueError("embedding model rejected malformed text")

        monkeypatch.setattr(mod, "index_chunks", _raise)
        dlq_calls = []
        monkeypatch.setattr(mod, "route_to_dlq_sync", lambda **kw: dlq_calls.append(kw))

        with pytest.raises(ValueError):
            mod.index_chunks_task.apply(args=[[{"chunk_id": "c1", "sha256": "a" * 64, "text": "x"}]]).get()

        assert len(dlq_calls) == 1
        assert dlq_calls[0]["category"] == FailureCategory.VECTOR_INGESTION
        assert dlq_calls[0]["task_name"] == "app.vectorstore.tasks.index_chunks_task"


# --------------------------------------------------------------------------
# app.ingestion.tasks.process_discovered_document_task /
# poll_sebi_sources_task
# --------------------------------------------------------------------------


class TestProcessDiscoveredDocumentTask:
    def _args(self) -> list:
        return [{"source_url": "https://sebi.gov.in/x.pdf", "source_kind": "rss", "title": "t"}, "new_document", b"%PDF-1.4".hex(), "a" * 64]

    def test_unsupported_file_error_is_never_retried(self, monkeypatch):
        import app.ingestion.tasks as mod
        from app.parsing.exceptions import UnsupportedFileError

        monkeypatch.setattr(mod, "get_settings", lambda: _settings())

        async def _raise(*a, **kw):
            raise UnsupportedFileError("not a PDF")

        monkeypatch.setattr(mod, "_process_one", _raise)
        dlq_calls = []
        monkeypatch.setattr(mod, "route_to_dlq_sync", lambda **kw: dlq_calls.append(kw))

        with pytest.raises(UnsupportedFileError):
            mod.process_discovered_document_task.apply(args=self._args()).get()

        assert len(dlq_calls) == 1
        assert dlq_calls[0]["category"] == FailureCategory.PDF_PARSING
        assert dlq_calls[0]["payload"]["content_sha256"] == "a" * 64

    def test_indexing_error_after_exhaustion_routes_to_vector_ingestion(self, monkeypatch):
        import app.ingestion.tasks as mod
        from app.parsing.exceptions import IndexingError

        monkeypatch.setattr(mod, "get_settings", lambda: _settings(retry_max_attempts_network=1))

        async def _raise(*a, **kw):
            raise IndexingError("Qdrant upsert failed")

        monkeypatch.setattr(mod, "_process_one", _raise)
        dlq_calls = []
        monkeypatch.setattr(mod, "route_to_dlq_sync", lambda **kw: dlq_calls.append(kw))

        with pytest.raises(IndexingError):
            mod.process_discovered_document_task.apply(args=self._args()).get()

        assert len(dlq_calls) == 1
        assert dlq_calls[0]["category"] == FailureCategory.VECTOR_INGESTION

    def test_transient_extraction_backend_error_is_retried_before_reaching_the_dlq(self, monkeypatch):
        import app.ingestion.tasks as mod
        from app.parsing.exceptions import ExtractionBackendError

        monkeypatch.setattr(mod, "get_settings", lambda: _settings(retry_max_attempts_network=4))

        call_count = {"n": 0}

        async def _raise(*a, **kw):
            call_count["n"] += 1
            # Mirrors app.parsing.extractor's real pattern: `raise
            # ExtractionBackendError(f"...: {primary_exc!r}") from primary_exc`
            # -- is_transient() walks __cause__, so the chain must be a
            # real `from`, not just the lower exception's repr embedded
            # in the message string (a string mention isn't a cause chain).
            try:
                raise httpx.ConnectError("refused")
            except httpx.ConnectError as inner:
                raise ExtractionBackendError(f"backend unreachable: {inner!r}") from inner

        monkeypatch.setattr(mod, "_process_one", _raise)
        dlq_calls = []
        monkeypatch.setattr(mod, "route_to_dlq_sync", lambda **kw: dlq_calls.append(kw))

        with pytest.raises(ExtractionBackendError):
            mod.process_discovered_document_task.apply(args=self._args()).get()

        assert call_count["n"] == 4
        assert len(dlq_calls) == 1


class TestPollSebiSourcesTask:
    def test_transient_poll_failure_is_retried_before_reaching_the_dlq(self, monkeypatch):
        import app.ingestion.tasks as mod

        monkeypatch.setattr(mod, "get_settings", lambda: _settings(retry_max_attempts_network=4))

        call_count = {"n": 0}

        async def _raise():
            call_count["n"] += 1
            raise httpx.ConnectError("refused")

        monkeypatch.setattr(mod, "_run_poll_cycle", _raise)
        dlq_calls = []
        monkeypatch.setattr(mod, "route_to_dlq_sync", lambda **kw: dlq_calls.append(kw))

        with pytest.raises(httpx.ConnectError):
            mod.poll_sebi_sources_task.apply().get()

        assert call_count["n"] == 4
        assert len(dlq_calls) == 1

    def test_exhausted_poll_failure_routes_to_rss_polling_category(self, monkeypatch):
        import app.ingestion.tasks as mod

        monkeypatch.setattr(mod, "get_settings", lambda: _settings(retry_max_attempts_network=1))

        async def _raise():
            raise httpx.ConnectError("refused")

        monkeypatch.setattr(mod, "_run_poll_cycle", _raise)
        dlq_calls = []
        monkeypatch.setattr(mod, "route_to_dlq_sync", lambda **kw: dlq_calls.append(kw))

        with pytest.raises(httpx.ConnectError):
            mod.poll_sebi_sources_task.apply().get()

        assert len(dlq_calls) == 1
        assert dlq_calls[0]["category"] == FailureCategory.RSS_POLLING
        assert dlq_calls[0]["payload"] == {}
