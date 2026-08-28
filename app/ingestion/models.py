"""Domain models for the SEBI ingestion pipeline."""
from __future__ import annotations

import datetime as dt
from enum import Enum

from pydantic import BaseModel, Field


class SourceKind(str, Enum):
    RSS = "rss"
    HTML_LISTING = "html_listing"


class ChangeKind(str, Enum):
    NEW_DOCUMENT = "new_document"
    CONTENT_AMENDED = "content_amended"  # same URL/circular number, different PDF hash
    UNCHANGED = "unchanged"


class DiscoveredDocument(BaseModel):
    """One circular/notification link found in a feed or listing page,
    prior to download. Not yet known to be new or changed."""

    source_url: str
    source_kind: SourceKind
    title: str
    published_at: dt.datetime | None = None
    circular_number: str | None = None


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
