"""Requirement 1 & 2's orchestrator: `SemanticParityChecker.check` takes
one circular's English and Hindi clause lists, aligns them
(app.translation_parity.alignment), runs the reused
app.localization.verification.CrossLingualVerifier per aligned pair,
and turns every failure mode into a typed `ClauseDiscrepancy` -- all
BEFORE `app.compiler` ever sees either language's clauses, per
Requirement 2's "before rule compilation."
"""
from __future__ import annotations

import logging
import uuid

from app.config import Settings
from app.localization.languages import RegionalLanguage
from app.localization.verification import CrossLingualVerifier
from app.models import ClauseChunk
from app.translation_parity.alignment import align_clauses
from app.translation_parity.models import (
    ClauseAlignment,
    ClauseDiscrepancy,
    DiscrepancySeverity,
    DiscrepancyType,
    TranslationParityReport,
)

logger = logging.getLogger(__name__)


class SemanticParityChecker:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._verifier = CrossLingualVerifier(settings)

    async def check(self, circular_number: str, english_clauses: list[ClauseChunk], hindi_clauses: list[ClauseChunk]) -> TranslationParityReport:
        alignments = await align_clauses(english_clauses, hindi_clauses, self._settings)

        discrepancies: list[ClauseDiscrepancy] = []
        similarities: list[float] = []

        for alignment in alignments:
            if alignment.english_clause is None or alignment.hindi_clause is None:
                discrepancies.append(self._missing_clause_discrepancy(alignment))
                continue

            verification = self._verifier.verify(
                alignment.english_clause.text, alignment.hindi_clause.text, RegionalLanguage.HINDI,
            )
            similarities.append(verification.semantic_similarity)

            if not verification.numeric_precision.numeric_precision_preserved:
                discrepancies.append(ClauseDiscrepancy(
                    discrepancy_type=DiscrepancyType.NUMERIC_MISMATCH, severity=DiscrepancySeverity.BLOCKING,
                    english_clause_number=alignment.english_clause.clause_number, hindi_clause_number=alignment.hindi_clause.clause_number,
                    description=(
                        f"Numeric values do not match between languages: English has "
                        f"{verification.numeric_precision.mismatched_source_values} with no Hindi counterpart; "
                        f"Hindi has {verification.numeric_precision.mismatched_translated_values} with no English counterpart."
                    ),
                    english_excerpt=alignment.english_clause.text, hindi_excerpt=alignment.hindi_clause.text,
                    verification=verification,
                ))

            if not verification.semantic_similarity_passed:
                discrepancies.append(ClauseDiscrepancy(
                    discrepancy_type=DiscrepancyType.SEMANTIC_DRIFT, severity=DiscrepancySeverity.BLOCKING,
                    english_clause_number=alignment.english_clause.clause_number, hindi_clause_number=alignment.hindi_clause.clause_number,
                    description=f"Cross-lingual semantic similarity ({verification.semantic_similarity:.3f}) is below the minimum threshold ({self._settings.localization_similarity_threshold}); the Hindi text may not faithfully render the English clause's meaning.",
                    english_excerpt=alignment.english_clause.text, hindi_excerpt=alignment.hindi_clause.text,
                    verification=verification,
                ))
            elif verification.semantic_similarity < self._settings.translation_parity_ambiguous_similarity_threshold:
                discrepancies.append(ClauseDiscrepancy(
                    discrepancy_type=DiscrepancyType.AMBIGUOUS_TRANSLATION, severity=DiscrepancySeverity.ADVISORY,
                    english_clause_number=alignment.english_clause.clause_number, hindi_clause_number=alignment.hindi_clause.clause_number,
                    description=f"Cross-lingual semantic similarity ({verification.semantic_similarity:.3f}) clears the hard floor but is low enough to warrant a human glance before compilation.",
                    english_excerpt=alignment.english_clause.text, hindi_excerpt=alignment.hindi_clause.text,
                    verification=verification,
                ))

        mean_similarity = sum(similarities) / len(similarities) if similarities else None
        requires_hitl_review = any(d.severity == DiscrepancySeverity.BLOCKING for d in discrepancies)

        report = TranslationParityReport(
            report_id=str(uuid.uuid4()), circular_number=circular_number,
            alignments=alignments, discrepancies=discrepancies,
            mean_semantic_similarity=mean_similarity, requires_hitl_review=requires_hitl_review,
        )
        logger.info(
            "Translation parity check for circular %s: %d clause pair(s), %d discrepanc(y/ies), requires_hitl_review=%s",
            circular_number, len([a for a in alignments if a.english_clause and a.hindi_clause]), len(discrepancies), requires_hitl_review,
        )
        return report

    @staticmethod
    def _missing_clause_discrepancy(alignment: ClauseAlignment) -> ClauseDiscrepancy:
        if alignment.hindi_clause is None:
            return ClauseDiscrepancy(
                discrepancy_type=DiscrepancyType.MISSING_CLAUSE_IN_HINDI, severity=DiscrepancySeverity.BLOCKING,
                english_clause_number=alignment.english_clause.clause_number, hindi_clause_number=None,
                description=f"English Clause {alignment.english_clause.clause_number} has no corresponding clause in the Hindi circular.",
                english_excerpt=alignment.english_clause.text,
            )
        return ClauseDiscrepancy(
            discrepancy_type=DiscrepancyType.MISSING_CLAUSE_IN_ENGLISH, severity=DiscrepancySeverity.BLOCKING,
            english_clause_number=None, hindi_clause_number=alignment.hindi_clause.clause_number,
            description=f"Hindi Clause {alignment.hindi_clause.clause_number} has no corresponding clause in the English circular.",
            hindi_excerpt=alignment.hindi_clause.text,
        )
