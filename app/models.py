"""Domain models shared across the parsing, chunking, and indexing layers."""
from __future__ import annotations

import datetime as dt
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from app.regulatory.taxonomy import DocumentType, Regulator


class ElementKind(str, Enum):
    TITLE = "title"
    SECTION_HEADER = "section_header"
    CLAUSE = "clause"
    NARRATIVE_TEXT = "narrative_text"
    TABLE = "table"
    FOOTNOTE = "footnote"
    LIST_ITEM = "list_item"
    UNCATEGORIZED = "uncategorized"


class BoundingBox(BaseModel):
    page_number: int | None = None
    coordinates: list[tuple[float, float]] | None = None


class DocumentElement(BaseModel):
    """A single layout-aware element extracted from the PDF, prior to chunking."""

    element_id: str
    kind: ElementKind
    text: str
    clause_number: str | None = None
    section_path: list[str] = Field(default_factory=list)
    page_number: int | None = None
    bbox: BoundingBox | None = None
    is_footnote_ref: bool = False


class CircularMetadata(BaseModel):
    """Header-level metadata for a regulatory document (SEBI circular, RBI
    Master Direction, IRDAI regulation, PFRDA circular, ...), either
    supplied by the caller or inferred from the first page during
    extraction. The field name `circular_number` predates multi-regulator
    support and is kept for backward compatibility -- it now holds
    whichever document-numbering convention `regulator` actually uses
    (an RBI Master Direction number, an IRDAI regulation number, etc.),
    not literally a SEBI circular number. `regulator` and `document_type`
    are what app.compiler.naming and app.agents.crew key their
    regulator-specific behavior off of -- see app.regulatory.taxonomy for
    the full taxonomy and detection logic."""

    circular_number: str | None = None
    issue_date: dt.date | None = None
    title: str | None = None
    source_filename: str | None = None
    department: str | None = None
    regulator: Regulator = Regulator.SEBI
    document_type: DocumentType = DocumentType.CIRCULAR


class ClauseChunk(BaseModel):
    """A semantically coherent, self-contained unit of legal text ready for
    embedding and retrieval."""

    chunk_id: str
    sha256: str
    text: str
    clause_number: str | None = None
    section_path: list[str] = Field(default_factory=list)
    section_title: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    footnotes: list[str] = Field(default_factory=list)
    contains_table: bool = False
    circular_number: str | None = None
    issue_date: dt.date | None = None
    source_filename: str | None = None
    regulator: Regulator = Regulator.SEBI
    document_type: DocumentType = DocumentType.CIRCULAR
    extra: dict[str, Any] = Field(default_factory=dict)


class ParseResult(BaseModel):
    metadata: CircularMetadata
    chunks: list[ClauseChunk]
    element_count: int
    warnings: list[str] = Field(default_factory=list)


class IndexRequest(BaseModel):
    chunks: list[ClauseChunk]
    recreate_collection: bool = False


class IndexResponse(BaseModel):
    collection: str
    upserted: int
    skipped_duplicates: int
