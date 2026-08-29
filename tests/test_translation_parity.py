"""Tests for the cross-lingual translation parity checker
(app.translation_parity). Follows tests/test_localization.py's stated
convention: the real multilingual embedding model
(paraphrase-multilingual-MiniLM-L12-v2) is exercised directly rather
than mocked, since the whole point is that real cross-lingual
similarity scoring behaves correctly -- only the Redis client is a
hand-rolled fake (matching tests/test_agent_graph.py's convention).
"""
from __future__ import annotations

import pytest

from app.config import get_settings
from app.models import ClauseChunk
from app.translation_parity.alignment import align_clauses
from app.translation_parity.checker import SemanticParityChecker
from app.translation_parity.diff_rendering import render_side_by_side_diff
from app.translation_parity.models import DiscrepancyReviewStatus, DiscrepancyType
from app.translation_parity.numeric_extraction import find_numeric_spans
from app.translation_parity.queue import TranslationDiscrepancyQueue

# Real SEBI-style margin clause, English + a faithful Hindi translation
# (numbers preserved) + a corrupted Hindi translation (20% -> 50%) --
# same style of fixture as tests/test_localization.py's HINDI_MARGIN_CLAUSE.
ENGLISH_MARGIN_CLAUSE = "Every stock broker shall maintain upfront margin of not less than 20% of the transaction value for all cash market trades."
HINDI_MARGIN_CLAUSE_FAITHFUL = "प्रत्येक स्टॉक ब्रोकर को सभी नकद बाजार लेनदेन के लिए लेनदेन मूल्य का न्यूनतम 20% अग्रिम मार्जिन बनाए रखना होगा।"
HINDI_MARGIN_CLAUSE_CORRUPTED = "प्रत्येक स्टॉक ब्रोकर को सभी नकद बाजार लेनदेन के लिए लेनदेन मूल्य का न्यूनतम 50% अग्रिम मार्जिन बनाए रखना होगा।"
HINDI_UNRELATED_CLAUSE = "प्रत्येक निवेशक शिकायत का समाधान तीस दिनों के भीतर किया जाना चाहिए।"  # "every investor complaint must be resolved within thirty days" -- unrelated topic


def _clause(clause_number: str | None, text: str) -> ClauseChunk:
    return ClauseChunk(chunk_id=f"c-{clause_number}", sha256="a" * 64, text=text, clause_number=clause_number)


class TestNumericExtraction:
    def test_finds_english_and_hindi_numeric_spans_with_correct_offsets(self) -> None:
        text = "Margin of 20% must be maintained; penalty is INR 50,000 per day."
        spans = find_numeric_spans(text)
        values = [s.normalized_value for s in spans]
        assert 20.0 in values
        assert 50000.0 in values
        for span in spans:
            assert text[span.start:span.end] == span.raw_match

    def test_finds_devanagari_digit_spans(self) -> None:
        spans = find_numeric_spans(HINDI_MARGIN_CLAUSE_FAITHFUL)
        assert any(s.normalized_value == 20.0 for s in spans)


class TestClauseAlignment:
    @pytest.mark.asyncio
    async def test_exact_clause_number_match_needs_no_embedding_call(self) -> None:
        settings = get_settings()
        english = [_clause("4.2.b", ENGLISH_MARGIN_CLAUSE)]
        hindi = [_clause("4.2.b", HINDI_MARGIN_CLAUSE_FAITHFUL)]

        alignments = await align_clauses(english, hindi, settings)

        assert len(alignments) == 1
        assert alignments[0].match_method == "clause_number"
        assert alignments[0].match_confidence == 1.0

    @pytest.mark.asyncio
    async def test_embedding_fallback_pairs_semantically_similar_clauses_without_numbering(self) -> None:
        settings = get_settings()
        english = [_clause(None, ENGLISH_MARGIN_CLAUSE)]
        hindi = [_clause(None, HINDI_MARGIN_CLAUSE_FAITHFUL)]

        alignments = await align_clauses(english, hindi, settings)

        assert len(alignments) == 1
        assert alignments[0].match_method == "embedding"
        assert alignments[0].english_clause is not None and alignments[0].hindi_clause is not None

    @pytest.mark.asyncio
    async def test_unrelated_clauses_are_left_unmatched(self) -> None:
        settings = get_settings()
        english = [_clause(None, ENGLISH_MARGIN_CLAUSE)]
        hindi = [_clause(None, HINDI_UNRELATED_CLAUSE)]

        alignments = await align_clauses(english, hindi, settings)

        assert len(alignments) == 2
        assert {a.match_method for a in alignments} == {"unmatched"}
        assert {a.english_clause is not None for a in alignments} == {True, False}


@pytest.mark.asyncio
class TestSemanticParityChecker:
    async def test_faithful_translation_with_matching_numbers_has_no_discrepancies(self) -> None:
        checker = SemanticParityChecker(get_settings())
        english = [_clause("4.2.b", ENGLISH_MARGIN_CLAUSE)]
        hindi = [_clause("4.2.b", HINDI_MARGIN_CLAUSE_FAITHFUL)]

        report = await checker.check("SEBI/HO/MIRSD/2024/100", english, hindi)

        assert report.discrepancies == []
        assert report.requires_hitl_review is False
        assert report.mean_semantic_similarity is not None and report.mean_semantic_similarity > 0

    async def test_corrupted_number_is_flagged_as_numeric_mismatch(self) -> None:
        checker = SemanticParityChecker(get_settings())
        english = [_clause("4.2.b", ENGLISH_MARGIN_CLAUSE)]
        hindi = [_clause("4.2.b", HINDI_MARGIN_CLAUSE_CORRUPTED)]

        report = await checker.check("SEBI/HO/MIRSD/2024/100", english, hindi)

        assert report.requires_hitl_review is True
        numeric_findings = [d for d in report.discrepancies if d.discrepancy_type == DiscrepancyType.NUMERIC_MISMATCH]
        assert len(numeric_findings) == 1
        assert 20.0 in numeric_findings[0].verification.numeric_precision.mismatched_source_values
        assert 50.0 in numeric_findings[0].verification.numeric_precision.mismatched_translated_values

    async def test_missing_hindi_clause_is_flagged(self) -> None:
        checker = SemanticParityChecker(get_settings())
        english = [_clause("4.2.b", ENGLISH_MARGIN_CLAUSE), _clause("4.3", "A second English-only clause with no Hindi counterpart.")]
        hindi = [_clause("4.2.b", HINDI_MARGIN_CLAUSE_FAITHFUL)]

        report = await checker.check("SEBI/HO/MIRSD/2024/100", english, hindi)

        assert report.requires_hitl_review is True
        missing = [d for d in report.discrepancies if d.discrepancy_type == DiscrepancyType.MISSING_CLAUSE_IN_HINDI]
        assert len(missing) == 1
        assert missing[0].english_clause_number == "4.3"


class TestDiffRendering:
    def test_mismatched_value_is_highlighted_red_matched_is_green(self) -> None:
        from app.localization.numeric_precision import check_numeric_precision

        numeric_result = check_numeric_precision(ENGLISH_MARGIN_CLAUSE, HINDI_MARGIN_CLAUSE_CORRUPTED)
        html = render_side_by_side_diff(ENGLISH_MARGIN_CLAUSE, HINDI_MARGIN_CLAUSE_CORRUPTED, numeric_result, english_clause_number="4.2.b", hindi_clause_number="4.2.b")

        assert "mismatched" in html or "#f8d7da" in html
        assert "20" in html and "50" in html

    def test_missing_clause_renders_placeholder(self) -> None:
        html = render_side_by_side_diff(ENGLISH_MARGIN_CLAUSE, None, None, english_clause_number="4.3", hindi_clause_number=None)
        assert "no corresponding clause" in html


class _FakeRedis:
    def __init__(self) -> None:
        self.strings: dict[str, str] = {}
        self.sets: dict[str, set[str]] = {}

    async def set(self, key: str, value: str) -> None:
        self.strings[key] = value

    async def get(self, key: str) -> str | None:
        return self.strings.get(key)

    async def sadd(self, key: str, member: str) -> None:
        self.sets.setdefault(key, set()).add(member)

    async def srem(self, key: str, member: str) -> None:
        self.sets.get(key, set()).discard(member)

    async def smembers(self, key: str) -> set[str]:
        return set(self.sets.get(key, set()))


@pytest.mark.asyncio
class TestTranslationDiscrepancyQueue:
    async def test_enqueue_get_list_and_resolve_round_trip(self) -> None:
        checker = SemanticParityChecker(get_settings())
        report = await checker.check(
            "SEBI/HO/MIRSD/2024/100",
            [_clause("4.2.b", ENGLISH_MARGIN_CLAUSE)],
            [_clause("4.2.b", HINDI_MARGIN_CLAUSE_CORRUPTED)],
        )
        queue = TranslationDiscrepancyQueue(_FakeRedis(), key_prefix="regengine:translation_parity")

        case = await queue.enqueue(report, {"4.2.b|4.2.b": "<table>diff</table>"})
        assert case.status == DiscrepancyReviewStatus.PENDING

        fetched = await queue.get(case.case_id)
        assert fetched is not None and fetched.report.circular_number == "SEBI/HO/MIRSD/2024/100"

        pending = await queue.list_pending()
        assert len(pending) == 1 and pending[0].case_id == case.case_id

        resolved = await queue.resolve(case.case_id, DiscrepancyReviewStatus.APPROVED, "officer-1", "Confirmed genuine mistranslation.")
        assert resolved.status == DiscrepancyReviewStatus.APPROVED
        assert resolved.resolved_by == "officer-1"

        pending_after = await queue.list_pending()
        assert pending_after == []

    async def test_resolving_twice_raises(self) -> None:
        checker = SemanticParityChecker(get_settings())
        report = await checker.check("X/1", [_clause("1", ENGLISH_MARGIN_CLAUSE)], [_clause("1", HINDI_MARGIN_CLAUSE_FAITHFUL)])
        queue = TranslationDiscrepancyQueue(_FakeRedis(), key_prefix="regengine:translation_parity")
        case = await queue.enqueue(report, {})

        await queue.resolve(case.case_id, DiscrepancyReviewStatus.DISMISSED, "officer-1", None)
        with pytest.raises(ValueError):
            await queue.resolve(case.case_id, DiscrepancyReviewStatus.APPROVED, "officer-1", None)

    async def test_resolving_unknown_case_raises_key_error(self) -> None:
        queue = TranslationDiscrepancyQueue(_FakeRedis(), key_prefix="regengine:translation_parity")
        with pytest.raises(KeyError):
            await queue.resolve("nonexistent", DiscrepancyReviewStatus.APPROVED, "officer-1", None)


class TestRouterWiring:
    def test_translation_parity_router_is_registered_on_the_app(self) -> None:
        import app.main as main_module

        # FastAPI 0.141 wraps `include_router()` results lazily
        # (`_IncludedRouter`, resolved only when the OpenAPI schema or an
        # actual request needs them) -- `app.routes` no longer flattens
        # to `APIRoute` objects up front, so the schema is the reliable
        # way to confirm a router was actually registered.
        paths = set(main_module.app.openapi()["paths"].keys())
        assert "/v1/translation-parity/check" in paths
        assert "/v1/translation-parity/discrepancies" in paths
        assert "/v1/translation-parity/discrepancies/{case_id}" in paths
        assert "/v1/translation-parity/discrepancies/{case_id}/resolve" in paths
