"""Unit tests for the pure-Python parsing logic (hierarchy, chunking, hashing).

These deliberately avoid the heavy Unstructured/Tika/embedding backends so
they run fast and without external services, by constructing DocumentElement
lists directly rather than parsing an actual PDF.
"""
from __future__ import annotations

import datetime as dt

from app.config import Settings
from app.models import CircularMetadata, DocumentElement, ElementKind
from app.parsing.chunker import chunk_elements
from app.parsing.hashing import sha256_of_clause, sha256_of_text
from app.parsing.hierarchy import HierarchyTracker, detect_clause_number, is_section_header


def test_detect_clause_number_variants() -> None:
    assert detect_clause_number("1. Applicability").clause_number == "1"
    assert detect_clause_number("2.1 Scope of this circular").clause_number == "2.1"
    assert detect_clause_number("2.1.b Additional disclosure required").clause_number == "2.1.b"
    assert detect_clause_number("2.1.(b) Additional disclosure required").clause_number == "2.1.b"
    assert detect_clause_number("(iii) any other matter").clause_number == "iii"
    assert detect_clause_number("Not numbered text") is None


def test_hierarchy_tracker_builds_section_path() -> None:
    tracker = HierarchyTracker()
    assert tracker.update("1", 1) == ["1"]
    assert tracker.update("1.1", 2) == ["1", "1.1"]
    assert tracker.update("1.1.a", 3) == ["1", "1.1", "1.1.a"]
    # sibling at depth 2 should pop the depth-3 clause
    assert tracker.update("1.2", 2) == ["1", "1.2"]
    # new top-level section resets everything below it
    assert tracker.update("2", 1) == ["2"]


def test_sha256_is_deterministic_and_scoped_to_circular() -> None:
    h1 = sha256_of_clause(circular_number="SEBI/HO/1", clause_number="2.1.b", text="Entities shall report.")
    h2 = sha256_of_clause(circular_number="SEBI/HO/1", clause_number="2.1.b", text="Entities shall report.")
    h3 = sha256_of_clause(circular_number="SEBI/HO/2", clause_number="2.1.b", text="Entities shall report.")
    assert h1 == h2
    assert h1 != h3
    assert len(h1) == 64


def test_sha256_of_text_normalizes_whitespace() -> None:
    assert sha256_of_text("a   b\n c") == sha256_of_text("a b c")


def _el(kind: ElementKind, text: str, clause_number: str | None, section_path: list[str], page: int = 1) -> DocumentElement:
    return DocumentElement(
        element_id="x",
        kind=kind,
        text=text,
        clause_number=clause_number,
        section_path=section_path,
        page_number=page,
    )


def test_chunking_keeps_clause_intact_and_attaches_footnote() -> None:
    elements = [
        _el(ElementKind.SECTION_HEADER, "Reporting Obligations", "2", ["2"]),
        _el(ElementKind.CLAUSE, "2.1 All intermediaries shall submit reports within 15 days of quarter end.", "2.1", ["2", "2.1"]),
        _el(ElementKind.CLAUSE, "2.1.b The report shall be filed in the format prescribed in Annexure A.", "2.1.b", ["2", "2.1", "2.1.b"]),
        _el(ElementKind.FOOTNOTE, "1. As amended by circular dated 1 Jan 2024.", None, ["2", "2.1", "2.1.b"]),
    ]
    metadata = CircularMetadata(circular_number="SEBI/HO/MRD/2024/1", issue_date=dt.date(2024, 1, 1))
    settings = Settings(chunk_min_chars=5)

    chunks = chunk_elements(elements, metadata, settings)

    assert len(chunks) >= 1
    clause_b_chunk = next(c for c in chunks if c.clause_number == "2.1.b")
    assert "Annexure A" in clause_b_chunk.text
    assert clause_b_chunk.footnotes and "amended" in clause_b_chunk.footnotes[0]
    assert clause_b_chunk.circular_number == "SEBI/HO/MRD/2024/1"
    assert len(clause_b_chunk.sha256) == 64


def test_is_section_header_heuristic() -> None:
    match = detect_clause_number("1. Applicability")
    assert is_section_header("1. Applicability", match) is True
    long_match = detect_clause_number("1. This is a much longer clause body that reads like actual obligation text.")
    assert is_section_header(
        "1. This is a much longer clause body that reads like actual obligation text.", long_match
    ) is False
