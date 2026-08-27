"""Semantic chunking tailored for legal/regulatory text.

Naive fixed-size chunking is unsafe for legal documents: splitting a clause
mid-sentence severs the obligation from its qualifier (e.g. separating
"shall submit within 15 days" from "unless an extension is granted under
2.1.c"), which corrupts retrieval and can misinform compliance decisions.

Strategy:
  1. Group consecutive DocumentElements under the same lowest-level clause
     number into a single candidate block (a clause is never split across
     chunks unless it alone exceeds `chunk_max_chars`).
  2. Attach any table immediately following a clause to that clause's chunk
     (tables are evidentiary, not standalone).
  3. Attach footnotes referenced within a chunk's page range to that chunk.
  4. Oversized clauses are split on sentence boundaries with a character
     overlap so context survives the cut; each fragment keeps full clause
     metadata (clause_number, section_path) so downstream systems can
     still cite the precise clause.
"""
from __future__ import annotations

import re
import uuid

from app.config import Settings
from app.models import CircularMetadata, ClauseChunk, DocumentElement, ElementKind
from app.parsing.exceptions import ChunkingError
from app.parsing.hashing import sha256_of_clause

_SENTENCE_BOUNDARY = re.compile(r"(?<=[.;:])\s+(?=[A-Z(])")


def _group_by_clause(elements: list[DocumentElement]) -> list[list[DocumentElement]]:
    groups: list[list[DocumentElement]] = []
    current: list[DocumentElement] = []
    current_clause: str | None = None

    for el in elements:
        if el.kind == ElementKind.FOOTNOTE:
            # Footnotes are attached to the current group rather than
            # starting a new one; if none is open yet, buffer as its own group.
            if current:
                current.append(el)
            else:
                groups.append([el])
            continue

        if el.kind in (ElementKind.CLAUSE, ElementKind.SECTION_HEADER):
            if el.clause_number != current_clause and current:
                groups.append(current)
                current = []
            current_clause = el.clause_number
            current.append(el)
            continue

        # Table / narrative / list-item / uncategorized: attach to the open
        # group (continuation of the current clause's content).
        if current:
            current.append(el)
        else:
            current = [el]
            current_clause = el.clause_number

    if current:
        groups.append(current)
    return groups


def _split_long_text(text: str, max_chars: int, overlap_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]

    sentences = _SENTENCE_BOUNDARY.split(text)
    fragments: list[str] = []
    buf = ""
    for sentence in sentences:
        candidate = f"{buf} {sentence}".strip() if buf else sentence
        if len(candidate) <= max_chars:
            buf = candidate
            continue
        if buf:
            fragments.append(buf)
        # carry a tail-overlap forward for context continuity
        tail = buf[-overlap_chars:] if buf else ""
        buf = f"{tail} {sentence}".strip() if tail else sentence
        if len(buf) > max_chars:
            # single sentence longer than max_chars: hard-wrap as last resort
            for i in range(0, len(buf), max_chars):
                fragments.append(buf[i : i + max_chars])
            buf = ""
    if buf:
        fragments.append(buf)
    return fragments or [text]


def _group_to_chunk_text(group: list[DocumentElement]) -> tuple[str, list[str], bool]:
    body_parts: list[str] = []
    footnotes: list[str] = []
    contains_table = False

    for el in group:
        if el.kind == ElementKind.FOOTNOTE:
            footnotes.append(el.text)
            continue
        if el.kind == ElementKind.TABLE:
            contains_table = True
            body_parts.append(f"[TABLE]\n{el.text}\n[/TABLE]")
            continue
        body_parts.append(el.text)

    return "\n".join(p for p in body_parts if p.strip()), footnotes, contains_table


def chunk_elements(
    elements: list[DocumentElement],
    metadata: CircularMetadata,
    settings: Settings,
) -> list[ClauseChunk]:
    """Turn a flat, ordered list of DocumentElements into clause-aware chunks."""
    if not elements:
        raise ChunkingError("No elements provided for chunking.")

    groups = _group_by_clause(elements)
    chunks: list[ClauseChunk] = []

    for group in groups:
        text, footnotes, contains_table = _group_to_chunk_text(group)
        text = text.strip()
        if len(text) < settings.chunk_min_chars and not contains_table:
            continue

        anchor = next((e for e in group if e.clause_number), group[0])
        section_path = anchor.section_path
        section_title = section_path[-1] if section_path else None
        pages = [e.page_number for e in group if e.page_number is not None]
        page_start = min(pages) if pages else None
        page_end = max(pages) if pages else None

        fragments = _split_long_text(text, settings.chunk_max_chars, settings.chunk_overlap_chars)

        for idx, fragment in enumerate(fragments):
            chunk_id = str(uuid.uuid4())
            digest = sha256_of_clause(
                circular_number=metadata.circular_number,
                clause_number=anchor.clause_number,
                text=fragment,
            )
            chunks.append(
                ClauseChunk(
                    chunk_id=chunk_id,
                    sha256=digest,
                    text=fragment,
                    clause_number=anchor.clause_number,
                    section_path=section_path,
                    section_title=section_title,
                    page_start=page_start,
                    page_end=page_end,
                    footnotes=footnotes if idx == 0 else [],
                    contains_table=contains_table and idx == 0,
                    circular_number=metadata.circular_number,
                    issue_date=metadata.issue_date,
                    source_filename=metadata.source_filename,
                    extra={"fragment_index": idx, "fragment_count": len(fragments)},
                )
            )

    if not chunks:
        raise ChunkingError("Chunking produced zero chunks from a non-empty element set.")

    return chunks
