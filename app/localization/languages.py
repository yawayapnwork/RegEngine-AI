"""The regional languages this pipeline supports, and the per-backend
language codes each downstream tool needs -- OCR engines, translation
models, and `langdetect` each use a DIFFERENT code for the same
language (ISO 639-1 "hi" vs. NLLB's "hin_Deva" vs. Tesseract's "hin"),
so this is the single place that mapping lives rather than scattered
string literals across the pipeline.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class RegionalLanguage(str, Enum):
    ENGLISH = "en"
    HINDI = "hi"
    MARATHI = "mr"
    GUJARATI = "gu"


class OCRBackendChoice(str, Enum):
    PADDLEOCR = "paddleocr"
    TESSERACT = "tesseract"


@dataclass(frozen=True)
class LanguageProfile:
    language: RegionalLanguage
    display_name: str
    script: str
    langdetect_code: str  # what `langdetect.detect()` returns for this language
    tesseract_lang_code: str  # traineddata filename stem, e.g. "hin" -> hin.traineddata
    nllb_code: str  # FLORES-200 code NLLB/IndicTrans2 use for src_lang/tgt_lang
    preferred_ocr_backend: OCRBackendChoice
    # PaddleOCR's own `lang=` model-selection code, or None if PaddleOCR
    # ships no model for this script at all (see this module's docstring
    # on Gujarati) and Tesseract is the only option regardless of
    # `preferred_ocr_backend`'s fallback ordering.
    paddleocr_lang_code: str | None


# PaddleOCR ships one shared "devanagari" recognition model covering
# Hindi, Marathi, Nepali, and Sanskrit (they share a script), selected
# via lang="devanagari" in PaddleOCR's own API -- NOT "hi"/"mr" as
# separate codes, a common integration mistake. PaddleOCR has never
# shipped a Gujarati-script model (as of the versions available at the
# time this was written) -- Gujarati therefore routes to Tesseract's
# `guj.traineddata` unconditionally; see app.localization.ocr's
# `select_ocr_backend` for where this table is actually consulted.
LANGUAGE_PROFILES: dict[RegionalLanguage, LanguageProfile] = {
    RegionalLanguage.ENGLISH: LanguageProfile(
        language=RegionalLanguage.ENGLISH, display_name="English", script="Latin",
        langdetect_code="en", tesseract_lang_code="eng", nllb_code="eng_Latn",
        preferred_ocr_backend=OCRBackendChoice.TESSERACT, paddleocr_lang_code="en",
    ),
    RegionalLanguage.HINDI: LanguageProfile(
        language=RegionalLanguage.HINDI, display_name="Hindi", script="Devanagari",
        langdetect_code="hi", tesseract_lang_code="hin", nllb_code="hin_Deva",
        preferred_ocr_backend=OCRBackendChoice.PADDLEOCR, paddleocr_lang_code="devanagari",
    ),
    RegionalLanguage.MARATHI: LanguageProfile(
        language=RegionalLanguage.MARATHI, display_name="Marathi", script="Devanagari",
        langdetect_code="mr", tesseract_lang_code="mar", nllb_code="mar_Deva",
        preferred_ocr_backend=OCRBackendChoice.PADDLEOCR, paddleocr_lang_code="devanagari",
    ),
    RegionalLanguage.GUJARATI: LanguageProfile(
        language=RegionalLanguage.GUJARATI, display_name="Gujarati", script="Gujarati",
        langdetect_code="gu", tesseract_lang_code="guj", nllb_code="guj_Gujr",
        preferred_ocr_backend=OCRBackendChoice.TESSERACT, paddleocr_lang_code=None,
    ),
}


def get_language_profile(language: RegionalLanguage) -> LanguageProfile:
    return LANGUAGE_PROFILES[language]


_LANGDETECT_TO_REGIONAL: dict[str, RegionalLanguage] = {p.langdetect_code: lang for lang, p in LANGUAGE_PROFILES.items()}


def regional_language_from_langdetect_code(code: str) -> RegionalLanguage | None:
    """`langdetect.detect()`'s ISO 639-1 code -> our RegionalLanguage, or
    None if the detected language isn't one this pipeline handles (a
    genuinely out-of-scope document should be routed to human review,
    not force-mapped to the nearest supported language)."""
    return _LANGDETECT_TO_REGIONAL.get(code)
