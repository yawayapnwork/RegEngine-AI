"""Domain models for the multi-regulator ingestion pipeline (SEBI, RBI,
IRDAI, PFRDA -- see app.regulatory.taxonomy and app.ingestion.regulator_sources)."""
from __future__ import annotations

import datetime as dt
from enum import Enum

from pydantic import BaseModel, Field

from app.regulatory.taxonomy import Regulator


class SourceKind(str, Enum):
    RSS = "rss"
    HTML_LISTING = "html_listing"


class ChangeKind(str, Enum):
    NEW_DOCUMENT = "new_document"
    CONTENT_AMENDED = "content_amended"  # same URL/circular number, different PDF hash
    UNCHANGED = "unchanged"


class DiscoveredDocument(BaseModel):
    """One circular/notification link found in a feed or listing page,
    prior to download. Not yet known to be new or changed.

    `regulator` is stamped by app.ingestion.feed_monitor from which
    regulator's configured source (app.ingestion.regulator_sources) this
    document was discovered under -- a deterministic ingestion-time fact,
    passed downstream as `source_tag` to app.parsing.extractor so the
    parser never has to re-derive it by sniffing the PDF's own header
    text (though it still can, as a fallback, for ad-hoc uploads with no
    known source)."""

    source_url: str
    source_kind: SourceKind
    title: str
    published_at: dt.datetime | None = None
    circular_number: str | None = None
    regulator: Regulator = Regulator.SEBI


class IngestedDocument(BaseModel):
    """A discovered document after its PDF bytes have been fetched and hashed."""

    discovered: DiscoveredDocument
    content_sha256: str
    content_length: int
    change_kind: ChangeKind


class IngestionRunResult(BaseModel):
    """Summary of one poll cycle across all configured sources."""

    started_at: dt.datetime
    finished_at: dt.datetime
    sources_polled: int
    documents_discovered: int
    documents_new: int
    documents_amended: int
    documents_failed: int
    errors: list[str] = Field(default_factory=list)
