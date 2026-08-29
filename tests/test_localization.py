"""Tests for app.localization: regional-Indian-language ingestion
support (Hindi, Marathi, Gujarati).

Real models are used wherever this environment allows -- NLLB
(facebook/nllb-200-distilled-600M) for translation and a real
multilingual sentence-embedding model
(sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2) for
cross-lingual semantic similarity -- rather than mocking either, since
the whole point of Requirements 1 and 3 is that the ACTUAL translation
and ACTUAL similarity scoring behave correctly on real regional-
language legal text. Both are loaded once per test session (see the
module-scoped fixtures below) since each is a real, non-trivial model
load. OCR (PaddleOCR/Tesseract) is NOT executable in this environment
(no OCR binary/model installed -- see app.localization.ocr's module
docstring) -- its tests cover routing logic and error-handling paths
that don't require the binary, plus one real test proving the
Tesseract-binary-missing case raises a typed, actionable error rather
than crashing or silently returning nothing.
"""
from __future__ import annotations

import shutil

import pytest

from app.config import Settings
from app.localization.entity_alignment import REGIONAL_ENTITY_ALIASES, align_regional_entity
from app.localization.languages import (
    LANGUAGE_PROFILES,
    OCRBackendChoice,
    RegionalLanguage,
    get_language_profile,
    regional_language_from_langdetect_code,
)
from app.localization.numeric_precision import check_numeric_precision, extract_numeric_tokens
from app.localization.ocr import OCRBackendError, extract_text_paddleocr, extract_text_tesseract, select_ocr_backend
from app.localization.pipeline import (
    UnsupportedLanguageError,
    build_translated_clause_chunk,
    detect_regional_language,
    process_regional_text,
)
from app.localization.verification import CrossLingualVerifier, compute_cross_lingual_similarity

HINDI_MARGIN_CLAUSE = "प्रत्येक स्टॉक ब्रोकर को लेनदेन मूल्य के कम से कम 20% के बराबर अग्रिम मार्जिन बनाए रखना होगा।"
MARATHI_MARGIN_CLAUSE = "प्रत्येक स्टॉक ब्रोकरने व्यवहार मूल्याच्या किमान 20% इतका आगाऊ मार्जिन राखला पाहिजे."
GUJARATI_MARGIN_CLAUSE = "દરેક સ્ટોક બ્રોકરે વ્યવહાર મૂલ્યના ઓછામાં ઓછા 20% જેટલું અગાઉથી માર્જિન જાળવવું જોઈએ."
ENGLISH_MARGIN_CLAUSE = "Every stock broker shall maintain upfront margin of not less than 20% of the transaction value."


# --------------------------------------------------------------------------
# languages.py
# --------------------------------------------------------------------------


class TestLanguageProfiles:
    def test_every_regional_language_has_a_profile(self):
        for lang in RegionalLanguage:
            assert lang in LANGUAGE_PROFILES

    def test_gujarati_has_no_paddleocr_model(self):
        assert get_language_profile(RegionalLanguage.GUJARATI).paddleocr_lang_code is None

    def test_hindi_and_marathi_share_the_devanagari_paddleocr_model(self):
        assert get_language_profile(RegionalLanguage.HINDI).paddleocr_lang_code == "devanagari"
        assert get_language_profile(RegionalLanguage.MARATHI).paddleocr_lang_code == "devanagari"

    def test_langdetect_code_round_trips(self):
        assert regional_language_from_langdetect_code("hi") == RegionalLanguage.HINDI
        assert regional_language_from_langdetect_code("mr") == RegionalLanguage.MARATHI
        assert regional_language_from_langdetect_code("gu") == RegionalLanguage.GUJARATI
        assert regional_language_from_langdetect_code("fr") is None  # out of scope for this pipeline


# --------------------------------------------------------------------------
# numeric_precision.py
# --------------------------------------------------------------------------


class TestNumericPrecision:
    def test_matching_translation_preserves_precision(self):
        result = check_numeric_precision(HINDI_MARGIN_CLAUSE, ENGLISH_MARGIN_CLAUSE)
        assert result.numeric_precision_preserved is True
        assert result.mismatched_source_values == []
        assert result.mismatched_translated_values == []

    def test_drifted_number_is_detected(self):
        wrong = "Every stock broker shall maintain upfront margin of not less than 50% of the transaction value."
        result = check_numeric_precision(HINDI_MARGIN_CLAUSE, wrong)
        assert result.numeric_precision_preserved is False
        assert result.mismatched_source_values == [20.0]
        assert result.mismatched_translated_values == [50.0]

    def test_devanagari_digit_glyphs_are_parsed(self):
        tokens = extract_numeric_tokens("न्यूनतम निवल मूल्य ५ करोड़ रुपये होना चाहिए।")
        assert len(tokens) == 1
        assert tokens[0].normalized_value == 5.0

    def test_gujarati_digit_glyphs_are_parsed(self):
        tokens = extract_numeric_tokens("ન્યૂનતમ મૂલ્ય ૧૦ લાખ હોવું જોઈએ.")
        assert len(tokens) == 1
        assert tokens[0].normalized_value == 10.0

    def test_no_numbers_on_either_side_is_trivially_preserved(self):
        result = check_numeric_precision("कोई संख्या नहीं है।", "There are no numbers here.")
        assert result.numeric_precision_preserved is True


# --------------------------------------------------------------------------
# entity_alignment.py
# --------------------------------------------------------------------------


class TestEntityAlignment:
    def test_every_glossary_key_exists_in_sebi_taxonomy(self):
        from app.agents.tools import SEBI_ENTITY_TAXONOMY

        assert set(REGIONAL_ENTITY_ALIASES).issubset(set(SEBI_ENTITY_TAXONOMY))

    def test_exact_hindi_alias_resolves(self):
        result = align_regional_entity("स्टॉक ब्रोकर", RegionalLanguage.HINDI)
        assert result.normalized_entity == "Stockbroker"
        assert result.method == "direct_glossary"
        assert result.confidence == 1.0

    def test_exact_gujarati_alias_resolves(self):
        result = align_regional_entity("શેર દલાલ", RegionalLanguage.GUJARATI)
        assert result.normalized_entity == "Stockbroker"

    def test_exact_marathi_alias_resolves(self):
        result = align_regional_entity("गुंतवणूक सल्लागार", RegionalLanguage.MARATHI)
        assert result.normalized_entity == "Investment Adviser"

    def test_partial_phrase_still_resolves_via_fuzzy_containment(self):
        result = align_regional_entity("समभाग दलाल संस्था", RegionalLanguage.MARATHI)
        assert result.normalized_entity == "Stockbroker"

    def test_unresolved_phrase_without_translate_fn_is_unresolved(self):
        result = align_regional_entity("कोई अज्ञात इकाई", RegionalLanguage.HINDI)
        assert result.resolved is False
        assert result.method == "unresolved"

    def test_translated_fallback_resolves_via_english_taxonomy(self):
        result = align_regional_entity("कोई अज्ञात इकाई", RegionalLanguage.HINDI, translate_fn=lambda p: "custodian")
        assert result.normalized_entity == "Custodian"
        assert result.method == "translated_fallback"
        assert result.translated_phrase == "custodian"


# --------------------------------------------------------------------------
# ocr.py -- routing/error-handling only; no OCR binary available here.
# --------------------------------------------------------------------------


class TestOCRBackendRouting:
    def test_hindi_prefers_paddleocr(self):
        assert select_ocr_backend(RegionalLanguage.HINDI) == OCRBackendChoice.PADDLEOCR

    def test_gujarati_forced_to_tesseract_regardless_of_preference(self):
        assert select_ocr_backend(RegionalLanguage.GUJARATI, prefer_backend=OCRBackendChoice.PADDLEOCR) == OCRBackendChoice.TESSERACT

    def test_explicit_preference_is_honored_when_a_model_exists(self):
        assert select_ocr_backend(RegionalLanguage.HINDI, prefer_backend=OCRBackendChoice.TESSERACT) == OCRBackendChoice.TESSERACT

    def test_paddleocr_on_gujarati_raises_without_touching_the_package(self):
        # No `import paddleocr` needed for this to raise -- the language
        # profile itself declares no model exists (see this module's
        # docstring), so this is real, non-mocked behavior even without
        # paddleocr installed.
        with pytest.raises(OCRBackendError, match="no recognition model"):
            extract_text_paddleocr("does-not-matter.png", RegionalLanguage.GUJARATI)


class TestTesseractBackendMissingBinary:
    def test_missing_tesseract_binary_raises_typed_error(self, tmp_path):
        from PIL import Image

        image_path = tmp_path / "blank.png"
        Image.new("RGB", (100, 30), color="white").save(image_path)

        # This environment has pytesseract (the Python binding) but not
        # the Tesseract OCR binary itself -- a real, common
        # misconfiguration (see app.localization.ocr's module docstring)
        # this test proves is handled with a typed, actionable error.
        if shutil.which("tesseract") is not None:
            pytest.skip("A real tesseract binary IS on PATH in this environment; this test targets the missing-binary path specifically.")

        with pytest.raises(OCRBackendError, match="tesseract' binary was not found"):
            extract_text_tesseract(str(image_path), RegionalLanguage.HINDI)


# --------------------------------------------------------------------------
# translation.py + verification.py -- real models, loaded once per session.
# --------------------------------------------------------------------------


@pytest.fixture(scope="session")
def nllb_settings() -> Settings:
    return Settings(localization_enabled=True, localization_translation_backend="nllb")


class TestNLLBTranslation:
    def test_translates_hindi_margin_clause_preserving_the_number(self, nllb_settings):
        from app.localization.translation import get_translation_backend

        backend = get_translation_backend(nllb_settings)
        translated = backend.translate_text(HINDI_MARGIN_CLAUSE, RegionalLanguage.HINDI)

        assert "20%" in translated or "20 %" in translated
        assert "broker" in translated.lower()

    def test_translates_marathi_and_gujarati(self, nllb_settings):
        from app.localization.translation import get_translation_backend

        backend = get_translation_backend(nllb_settings)
        mr = backend.translate_text(MARATHI_MARGIN_CLAUSE, RegionalLanguage.MARATHI)
        gu = backend.translate_text(GUJARATI_MARGIN_CLAUSE, RegionalLanguage.GUJARATI)

        assert "20" in mr and "broker" in mr.lower()
        assert "20" in gu and "broker" in gu.lower()

    def test_unknown_backend_name_raises(self):
        with pytest.raises(ValueError, match="Unknown localization_translation_backend"):
            from app.localization.translation import get_translation_backend

            get_translation_backend(Settings(localization_translation_backend="bogus"))


class TestCrossLingualVerifier:
    def test_faithful_translation_scores_above_threshold(self, nllb_settings):
        similarity = compute_cross_lingual_similarity(HINDI_MARGIN_CLAUSE, ENGLISH_MARGIN_CLAUSE, nllb_settings.localization_similarity_model_id)
        assert similarity >= nllb_settings.localization_similarity_threshold

    def test_unrelated_text_scores_far_below_threshold(self, nllb_settings):
        unrelated = "शेयर बाजार में आज भारी गिरावट दर्ज की गई।"  # "The stock market recorded a sharp fall today." -- unrelated to the margin clause
        similarity = compute_cross_lingual_similarity(unrelated, ENGLISH_MARGIN_CLAUSE, nllb_settings.localization_similarity_model_id)
        assert similarity < nllb_settings.localization_similarity_threshold

    def test_semantic_similarity_alone_is_weak_on_numeric_drift(self, nllb_settings):
        """Documents the exact empirical finding
        app.localization.verification's module docstring cites: a
        translation that only changes the percentage still scores well
        above the threshold on semantic similarity alone -- proving
        Requirement 3's numeric-precision check is NOT redundant with
        semantic similarity scoring."""
        drifted_translation = "Every stock broker shall maintain upfront margin of not less than 50% of the transaction value."
        similarity = compute_cross_lingual_similarity(HINDI_MARGIN_CLAUSE, drifted_translation, nllb_settings.localization_similarity_model_id)
        assert similarity >= nllb_settings.localization_similarity_threshold  # still "passes" on similarity alone

    def test_verify_combines_both_checks_and_fails_on_numeric_drift(self, nllb_settings):
        verifier = CrossLingualVerifier(nllb_settings)
        drifted_translation = "Every stock broker shall maintain upfront margin of not less than 50% of the transaction value."

        result = verifier.verify(HINDI_MARGIN_CLAUSE, drifted_translation, RegionalLanguage.HINDI)

        assert result.semantic_similarity_passed is True  # semantic check alone would have let this through
        assert result.numeric_precision.numeric_precision_preserved is False
        assert result.passed is False  # combined verdict correctly fails
        assert any("Numeric mismatch" in r for r in result.reasons)

    def test_verify_passes_a_faithful_translation(self, nllb_settings):
        verifier = CrossLingualVerifier(nllb_settings)
        result = verifier.verify(HINDI_MARGIN_CLAUSE, ENGLISH_MARGIN_CLAUSE, RegionalLanguage.HINDI)
        assert result.passed is True
        assert result.reasons == []


# --------------------------------------------------------------------------
# pipeline.py -- end-to-end.
# --------------------------------------------------------------------------


class TestDetectRegionalLanguage:
    def test_detects_hindi(self):
        assert detect_regional_language(HINDI_MARGIN_CLAUSE) == RegionalLanguage.HINDI

    def test_detects_gujarati(self):
        assert detect_regional_language(GUJARATI_MARGIN_CLAUSE) == RegionalLanguage.GUJARATI

    def test_unrecognizable_text_returns_none(self):
        assert detect_regional_language("!@#$ %^&* 1234") is None


class TestProcessRegionalText:
    def test_end_to_end_hindi_clause(self, nllb_settings):
        result = process_regional_text(HINDI_MARGIN_CLAUSE, nllb_settings, entity_phrases=["स्टॉक ब्रोकर"])

        assert result.source_language == RegionalLanguage.HINDI
        assert "20%" in result.translated_text or "20 %" in result.translated_text
        assert result.verification.passed is True
        assert result.entity_alignments[0].normalized_entity == "Stockbroker"
        assert result.requires_human_review() is False

    def test_english_input_is_a_passthrough(self, nllb_settings):
        result = process_regional_text(ENGLISH_MARGIN_CLAUSE, nllb_settings, source_language=RegionalLanguage.ENGLISH)
        assert result.translated_text == ENGLISH_MARGIN_CLAUSE
        assert result.translation_backend == "none"
        assert result.verification.passed is True

    def test_undetectable_language_raises(self):
        with pytest.raises(UnsupportedLanguageError):
            process_regional_text("!@#$ %^&* 1234", Settings(localization_enabled=True))

    def test_build_translated_clause_chunk_carries_full_provenance(self, nllb_settings):
        result = process_regional_text(HINDI_MARGIN_CLAUSE, nllb_settings)
        chunk = build_translated_clause_chunk(chunk_id="c1", sha256="a" * 64, result=result, clause_number="3.2.1")

        assert chunk.text == result.translated_text
        assert chunk.clause_number == "3.2.1"
        assert chunk.extra["localization"]["source_language"] == "hi"
        assert chunk.extra["localization"]["original_text"] == HINDI_MARGIN_CLAUSE
        assert chunk.extra["localization"]["verification"]["passed"] is True
