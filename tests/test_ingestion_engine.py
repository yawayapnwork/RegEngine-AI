"""Ingestion Engine test suite: layout-aware parsing and semantic chunking
of a sample SEBI Master Circular, and downstream Qdrant indexing.

Both heavy backends are mocked at their call boundary rather than run for
real:
  - Tika (`app.parsing.extractor._partition_with_tika`) is monkeypatched to
    return canned "raw element" dicts derived from a realistic sample
    circular's text, so hierarchy detection / clause chunking / metadata
    extraction all run against real logic on real (if synthetic) input.
  - Qdrant (`app.vectorstore.qdrant_store._get_client`) and the embedding
    model (`app.vectorstore.qdrant_store.embed_texts`) are replaced with
    in-memory doubles, so no network call or GPU/CPU model load happens.
"""
from __future__ import annotations

import datetime as dt

import pytest
import pytest_asyncio

from app.config import Settings
from app.models import ClauseChunk
from app.parsing import extractor as extractor_module
from app.parsing.exceptions import EmbeddingError, IndexingError, UnsupportedFileError
from app.services.pipeline import parse_pdf_bytes
from app.vectorstore import qdrant_store as qdrant_store_module

# --------------------------------------------------------------------------
# Sample SEBI Master Circular text and the "raw Tika elements" it would
# produce (one dict per non-blank line, mirroring _partition_with_tika's
# actual output shape).
# --------------------------------------------------------------------------

SAMPLE_CIRCULAR_TEXT = """SEBI/HO/MRD/DP/CIR/P/2026/45

Master Circular for Stock Brokers

1 January 2026

1. Applicability
This circular applies to all stock brokers registered with SEBI.

2. Margin Requirements
2.1 Every stock broker shall maintain an upfront margin of not less than 20% of the transaction value for all cash market trades.
2.1.b The margin shall be reported to the exchange within 24 hours of trade execution, and shall ensure adequate internal controls are in place at all times.
4) As amended by circular dated 1 January 2025.

3. Reporting Obligations
3.1 Stock brokers shall submit a monthly compliance report within 7 days of month end.
"""
# NOTE: two deliberate fixture choices --
#   1. The footnote uses "4)" (paren, not dot). detect_clause_number()'s
#      top-level pattern ("digit + '.' + text") and is_footnote()'s
#      heuristic ("digit + '.'/')' + text") overlap almost completely for
#      the dot form, so a dot-numbered footnote is structurally
#      indistinguishable from a same-shaped section header on text content
#      alone (app.parsing.extractor now resolves that overlap in favor of
#      clause classification -- see its _build_document_elements fix). The
#      paren form is footnote-only under both heuristics.
#   2. It's placed directly after "2.1.b", not at the document's end --
#      chunker._group_by_clause() attaches a footnote element to whichever
#      clause group is still open when it's encountered in document order,
#      not by page number or a later "nearest clause" lookup.


def _raw_tika_elements(text: str) -> list[dict]:
    """Mirrors app.parsing.extractor._partition_with_tika's return shape."""
    return [
        {"text": line.strip(), "category": "UncategorizedText", "page_number": None, "coordinates": None, "text_as_html": None}
        for line in text.splitlines()
        if line.strip()
    ]


@pytest.fixture
def tika_settings() -> Settings:
    return Settings(extraction_backend="tika", tika_server_url="http://fake-tika:9998", chunk_min_chars=5)


@pytest.fixture
def mock_tika(monkeypatch: pytest.MonkeyPatch):
    """Replaces the Tika backend call with one that returns elements parsed
    from SAMPLE_CIRCULAR_TEXT, without requiring the `tika` package or a
    running Tika server."""
    calls: list[tuple[str, str]] = []

    def _fake_partition_with_tika(path: str, server_url: str) -> list[dict]:
        calls.append((path, server_url))
        return _raw_tika_elements(SAMPLE_CIRCULAR_TEXT)

    monkeypatch.setattr(extractor_module, "_partition_with_tika", _fake_partition_with_tika)
    return calls


# --------------------------------------------------------------------------
# Layout parsing
# --------------------------------------------------------------------------


class TestLayoutParsing:
    @pytest.mark.asyncio
    async def test_extract_pdf_detects_circular_metadata(self, tmp_path, tika_settings, mock_tika):
        pdf_bytes = b"%PDF-1.4\n%mock circular\n"
        metadata, elements = await extractor_module.extract_pdf(
            file_bytes=pdf_bytes,
            source_path=tmp_path / "circular.pdf",
            filename="circular.pdf",
            settings=tika_settings,
        )

        assert metadata.circular_number == "SEBI/HO/MRD/DP/CIR/P/2026/45"
        assert metadata.issue_date == dt.date(2026, 1, 1)
        assert metadata.source_filename == "circular.pdf"
        assert len(elements) > 0
        assert mock_tika  # backend was actually invoked

    @pytest.mark.asyncio
    async def test_extract_pdf_builds_layout_hierarchy(self, tmp_path, tika_settings, mock_tika):
        _, elements = await extractor_module.extract_pdf(
            file_bytes=b"%PDF-1.4\n",
            source_path=tmp_path / "circular.pdf",
            filename="circular.pdf",
            settings=tika_settings,
        )

        clause_2_1_b = next(e for e in elements if e.clause_number == "2.1.b")
        assert clause_2_1_b.section_path == ["2", "2.1", "2.1.b"]

        footnote = next(e for e in elements if e.is_footnote_ref)
        assert "amended" in footnote.text

    @pytest.mark.asyncio
    async def test_extract_pdf_rejects_non_pdf_bytes(self, tmp_path, tika_settings, mock_tika):
        with pytest.raises(UnsupportedFileError):
            await extractor_module.extract_pdf(
                file_bytes=b"not a pdf at all",
                source_path=tmp_path / "circular.pdf",
                filename="circular.pdf",
                settings=tika_settings,
            )


# --------------------------------------------------------------------------
# Semantic chunking, via the full parse_pdf_bytes service
# --------------------------------------------------------------------------


class TestSemanticChunking:
    @pytest.mark.asyncio
    async def test_parse_pdf_bytes_produces_clause_chunks(self, tika_settings, mock_tika):
        result = await parse_pdf_bytes(b"%PDF-1.4\n", filename="circular.pdf", settings=tika_settings)

        assert result.element_count > 0
        assert len(result.chunks) > 0

        margin_chunk = next(c for c in result.chunks if c.clause_number == "2.1")
        assert "20%" in margin_chunk.text
        assert margin_chunk.circular_number == "SEBI/HO/MRD/DP/CIR/P/2026/45"
        assert margin_chunk.issue_date == dt.date(2026, 1, 1)
        assert len(margin_chunk.sha256) == 64

    @pytest.mark.asyncio
    async def test_parse_pdf_bytes_attaches_footnote_to_nearest_clause(self, tika_settings, mock_tika):
        result = await parse_pdf_bytes(b"%PDF-1.4\n", filename="circular.pdf", settings=tika_settings)
        clause_b = next(c for c in result.chunks if c.clause_number == "2.1.b")
        assert clause_b.footnotes and "amended" in clause_b.footnotes[0]

    @pytest.mark.asyncio
    async def test_parse_pdf_bytes_reidentical_text_reidentical_hash(self, tika_settings, mock_tika):
        first = await parse_pdf_bytes(b"%PDF-1.4\n", filename="a.pdf", settings=tika_settings)
        second = await parse_pdf_bytes(b"%PDF-1.4\n", filename="b.pdf", settings=tika_settings)
        first_hashes = {c.clause_number: c.sha256 for c in first.chunks}
        second_hashes = {c.clause_number: c.sha256 for c in second.chunks}
        assert first_hashes == second_hashes  # deterministic, re-parse-safe


# --------------------------------------------------------------------------
# Qdrant indexing (mocked client + mocked embedder)
# --------------------------------------------------------------------------


class _FakeQdrantClient:
    """In-memory double for AsyncQdrantClient covering only the surface
    app.vectorstore.qdrant_store actually calls."""

    def __init__(self, *, collection_exists: bool = False, fail_upsert: bool = False) -> None:
        self._collection_exists = collection_exists
        self.fail_upsert = fail_upsert
        self.created_collections: list[str] = []
        self.payload_index_fields: list[str] = []
        self.upsert_batches: list[list] = []
        self.closed = False

    async def collection_exists(self, name: str) -> bool:
        return self._collection_exists

    async def delete_collection(self, name: str) -> None:
        self._collection_exists = False

    async def create_collection(self, collection_name: str, vectors_config) -> None:
        self.created_collections.append(collection_name)
        self._collection_exists = True

    async def create_payload_index(self, collection_name: str, field_name: str, field_schema) -> None:
        self.payload_index_fields.append(field_name)

    async def upsert(self, collection_name: str, points, wait: bool = True) -> None:
        if self.fail_upsert:
            raise RuntimeError("simulated Qdrant upsert failure")
        self.upsert_batches.append(list(points))

    async def close(self) -> None:
        self.closed = True


def _clause_chunk(chunk_id: str, sha256: str, text: str = "Some clause text.") -> ClauseChunk:
    return ClauseChunk(chunk_id=chunk_id, sha256=sha256, text=text, circular_number="SEBI/HO/MRD/DP/CIR/P/2026/45")


@pytest_asyncio.fixture
async def indexing_settings() -> Settings:
    return Settings(qdrant_collection="test_collection", qdrant_upsert_batch_size=2, embedding_dim=4)


class TestQdrantIndexing:
    @pytest.mark.asyncio
    async def test_index_chunks_creates_collection_when_missing(self, monkeypatch, indexing_settings):
        fake_client = _FakeQdrantClient(collection_exists=False)
        monkeypatch.setattr(qdrant_store_module, "_get_client", lambda settings: fake_client)
        monkeypatch.setattr(qdrant_store_module, "embed_texts", _fake_embed_texts)

        chunks = [_clause_chunk("c1", "a" * 64), _clause_chunk("c2", "b" * 64)]
        response = await qdrant_store_module.index_chunks(chunks, indexing_settings)

        assert fake_client.created_collections == ["test_collection"]
        assert set(fake_client.payload_index_fields) == {"circular_number", "clause_number", "sha256", "issue_date"}
        assert response.upserted == 2
        assert response.skipped_duplicates == 0
        assert fake_client.closed is True

    @pytest.mark.asyncio
    async def test_index_chunks_skips_existing_collection(self, monkeypatch, indexing_settings):
        fake_client = _FakeQdrantClient(collection_exists=True)
        monkeypatch.setattr(qdrant_store_module, "_get_client", lambda settings: fake_client)
        monkeypatch.setattr(qdrant_store_module, "embed_texts", _fake_embed_texts)

        await qdrant_store_module.index_chunks([_clause_chunk("c1", "a" * 64)], indexing_settings)

        assert fake_client.created_collections == []

    @pytest.mark.asyncio
    async def test_index_chunks_dedupes_by_sha256_within_batch(self, monkeypatch, indexing_settings):
        fake_client = _FakeQdrantClient(collection_exists=True)
        monkeypatch.setattr(qdrant_store_module, "_get_client", lambda settings: fake_client)
        monkeypatch.setattr(qdrant_store_module, "embed_texts", _fake_embed_texts)

        chunks = [_clause_chunk("c1", "a" * 64), _clause_chunk("c2", "a" * 64)]  # same sha256
        response = await qdrant_store_module.index_chunks(chunks, indexing_settings)

        assert response.upserted == 1
        assert response.skipped_duplicates == 1

    @pytest.mark.asyncio
    async def test_index_chunks_batches_upserts(self, monkeypatch, indexing_settings):
        fake_client = _FakeQdrantClient(collection_exists=True)
        monkeypatch.setattr(qdrant_store_module, "_get_client", lambda settings: fake_client)
        monkeypatch.setattr(qdrant_store_module, "embed_texts", _fake_embed_texts)

        chunks = [_clause_chunk(f"c{i}", f"{i:064d}") for i in range(5)]  # batch_size=2 -> 3 batches
        await qdrant_store_module.index_chunks(chunks, indexing_settings)

        assert len(fake_client.upsert_batches) == 3
        assert [len(b) for b in fake_client.upsert_batches] == [2, 2, 1]

    @pytest.mark.asyncio
    async def test_index_chunks_empty_input_is_a_noop(self, monkeypatch, indexing_settings):
        called = False

        def _get_client(settings):
            nonlocal called
            called = True
            return _FakeQdrantClient()

        monkeypatch.setattr(qdrant_store_module, "_get_client", _get_client)
        response = await qdrant_store_module.index_chunks([], indexing_settings)

        assert response.upserted == 0
        assert called is False  # never even opens a client for an empty batch

    @pytest.mark.asyncio
    async def test_index_chunks_wraps_upsert_failure_as_indexing_error(self, monkeypatch, indexing_settings):
        fake_client = _FakeQdrantClient(collection_exists=True, fail_upsert=True)
        monkeypatch.setattr(qdrant_store_module, "_get_client", lambda settings: fake_client)
        monkeypatch.setattr(qdrant_store_module, "embed_texts", _fake_embed_texts)

        with pytest.raises(IndexingError):
            await qdrant_store_module.index_chunks([_clause_chunk("c1", "a" * 64)], indexing_settings)
        assert fake_client.closed is True  # `finally` still releases the client

    @pytest.mark.asyncio
    async def test_index_chunks_propagates_embedding_error(self, monkeypatch, indexing_settings):
        fake_client = _FakeQdrantClient(collection_exists=True)
        monkeypatch.setattr(qdrant_store_module, "_get_client", lambda settings: fake_client)

        async def _failing_embed(texts, settings):
            raise EmbeddingError("simulated embedding backend outage")

        monkeypatch.setattr(qdrant_store_module, "embed_texts", _failing_embed)

        with pytest.raises(EmbeddingError):
            await qdrant_store_module.index_chunks([_clause_chunk("c1", "a" * 64)], indexing_settings)


async def _fake_embed_texts(texts: list[str], settings: Settings) -> list[list[float]]:
    return [[0.1, 0.2, 0.3, 0.4] for _ in texts]
