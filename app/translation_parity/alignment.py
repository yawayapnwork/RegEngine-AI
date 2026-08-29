"""Requirement 1's clause alignment step: pairs each English clause
with its Hindi counterpart before any semantic/numeric comparison can
run at all -- comparison requires knowing WHICH clause on one side
corresponds to WHICH clause on the other, a problem that does not exist
in app.localization (a single-document pipeline with no second side to
align against) or app.diffing.semantic_diff (aligns a new clause
against a historical INDEX via Qdrant, not two co-arriving documents).

Two-tier alignment, cheapest signal first:

  1. Exact clause_number match. SEBI's own Hindi and English releases
     of the same circular overwhelmingly preserve clause/paragraph
     numbering across languages (both are numbering the same legal
     document structure) -- when both sides carry a clause_number,
     matching on it is free, certain, and immune to translation
     quality entirely.
  2. Cross-lingual embedding similarity, for whatever remains
     unmatched after (1) (e.g. missing clause_number metadata, or a
     genuine structural split/merge between the two versions). Each
     side's leftover clauses are embedded with the SAME multilingual
     model app.localization.verification already uses
     (settings.localization_similarity_model_id) and greedily paired by
     descending cosine similarity, one to one -- mirroring
     app.diffing.semantic_diff's find-best-match pattern, but matching
     within this circular's own two clause sets rather than against a
     historical index.

Anything left unmatched after both tiers is a genuine candidate for a
MISSING_CLAUSE discrepancy -- app.translation_parity.checker turns an
unmatched clause into exactly that finding.
"""
from __future__ import annotations

from app.config import Settings
from app.models import ClauseChunk
from app.translation_parity.models import ClauseAlignment, ClauseRef

# Below this cosine similarity, an embedding "match" is no better than
# chance for two SEBI legal clauses in a shared multilingual embedding
# space -- refusing to pair two clauses that share no real
# correspondence is more useful than forcing a low-confidence pairing a
# reviewer would need to untangle from a genuine SEMANTIC_DRIFT finding.
_MIN_EMBEDDING_ALIGNMENT_SIMILARITY = 0.35


def _by_clause_number(clauses: list[ClauseChunk]) -> dict[str, ClauseChunk]:
    return {c.clause_number: c for c in clauses if c.clause_number}


def _to_ref(chunk: ClauseChunk | None) -> ClauseRef | None:
    return ClauseRef(clause_number=chunk.clause_number, text=chunk.text) if chunk is not None else None


async def align_clauses(
    english_clauses: list[ClauseChunk],
    hindi_clauses: list[ClauseChunk],
    settings: Settings,
) -> list[ClauseAlignment]:
    alignments: list[ClauseAlignment] = []

    english_by_number = _by_clause_number(english_clauses)
    hindi_by_number = _by_clause_number(hindi_clauses)
    matched_numbers = set(english_by_number) & set(hindi_by_number)

    for number in sorted(matched_numbers):
        alignments.append(ClauseAlignment(
            english_clause=_to_ref(english_by_number[number]), hindi_clause=_to_ref(hindi_by_number[number]),
            match_method="clause_number", match_confidence=1.0,
        ))

    remaining_english = [c for c in english_clauses if c.clause_number not in matched_numbers]
    remaining_hindi = [c for c in hindi_clauses if c.clause_number not in matched_numbers]

    embedding_alignments, unmatched_english, unmatched_hindi = await _align_by_embedding(remaining_english, remaining_hindi, settings)
    alignments.extend(embedding_alignments)

    for chunk in unmatched_english:
        alignments.append(ClauseAlignment(english_clause=_to_ref(chunk), hindi_clause=None, match_method="unmatched"))
    for chunk in unmatched_hindi:
        alignments.append(ClauseAlignment(english_clause=None, hindi_clause=_to_ref(chunk), match_method="unmatched"))

    return alignments


async def _align_by_embedding(
    english_clauses: list[ClauseChunk],
    hindi_clauses: list[ClauseChunk],
    settings: Settings,
) -> tuple[list[ClauseAlignment], list[ClauseChunk], list[ClauseChunk]]:
    if not english_clauses or not hindi_clauses:
        return [], list(english_clauses), list(hindi_clauses)

    from app.localization.verification import compute_cross_lingual_similarity

    # Full pairwise similarity matrix -- these clause lists are a single
    # circular's leftovers after exact-number matching, never the whole
    # corpus, so O(n*m) embedding calls stay small in practice (a
    # circular with dozens, not thousands, of clauses).
    scored: list[tuple[float, int, int]] = []
    for i, en in enumerate(english_clauses):
        for j, hi in enumerate(hindi_clauses):
            similarity = compute_cross_lingual_similarity(en.text, hi.text, settings.localization_similarity_model_id)
            scored.append((similarity, i, j))
    scored.sort(key=lambda t: t[0], reverse=True)

    used_english: set[int] = set()
    used_hindi: set[int] = set()
    alignments: list[ClauseAlignment] = []
    for similarity, i, j in scored:
        if i in used_english or j in used_hindi:
            continue
        if similarity < _MIN_EMBEDDING_ALIGNMENT_SIMILARITY:
            continue
        used_english.add(i)
        used_hindi.add(j)
        alignments.append(ClauseAlignment(
            english_clause=_to_ref(english_clauses[i]), hindi_clause=_to_ref(hindi_clauses[j]),
            match_method="embedding", match_confidence=similarity,
        ))

    unmatched_english = [c for idx, c in enumerate(english_clauses) if idx not in used_english]
    unmatched_hindi = [c for idx, c in enumerate(hindi_clauses) if idx not in used_hindi]
    return alignments, unmatched_english, unmatched_hindi
