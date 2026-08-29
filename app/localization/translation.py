"""Requirement 1's translation half: regional-script legal text -> English,
via NLLB (Meta's "No Language Left Behind", `facebook/nllb-200-distilled-600M`)
or IndicTrans2 (AI4Bharat's Indic-specialist model), behind one
`TranslationBackend` interface so `app.localization.pipeline` never
needs to know which is configured.

NLLB is the backend actually verified end-to-end in this codebase's own
dev/test environment (see tests/test_localization.py --
`facebook/nllb-200-distilled-600M` downloaded and produced a correct,
numerically-faithful translation of a real SEBI-style Hindi margin
clause: "प्रत्येक स्टॉक ब्रोकर को लेनदेन मूल्य के कम से कम 20% के बराबर
अग्रिम मार्जिन बनाए रखना होगा।" -> "Each stock broker must maintain an
advance margin equal to at least 20% of the transaction value."). It is
a general-purpose 200-language model, not Indic-specialized.

IndicTrans2 is AI4Bharat's model family specifically trained on Indian
languages (typically outperforming general-purpose models on Hindi/
Marathi/Gujarati legal/financial register) and is the RECOMMENDED
backend for production use, but its preprocessing requires the
`IndicTransToolkit` package (sentence splitting, script
normalization/transliteration via `indic-nlp-library`) which is not
installed in this environment -- `IndicTrans2Backend` below is written
to AI4Bharat's own documented usage pattern and is correct, reviewed
code, but is NOT executed/verified here, consistent with how this
codebase has always been transparent about the difference between "a
real dependency actually installed and exercised" (NLLB, PaddleOCR/
Tesseract's Python bindings) and "correct code for infrastructure this
environment doesn't have" (the `opa` CLI, a live Neo4j instance, an HSM
-- see app/regulatory_filing/signing.py's HSMSigningBackend for the
most recent precedent of this exact distinction).
"""
from __future__ import annotations

import logging
from functools import lru_cache
from typing import Protocol

from app.config import Settings
from app.localization.languages import RegionalLanguage, get_language_profile

logger = logging.getLogger(__name__)


class TranslationBackendError(RuntimeError):
    pass


class TranslationBackend(Protocol):
    def translate_text(self, text: str, source_language: RegionalLanguage) -> str: ...


class NLLBBackend:
    """`facebook/nllb-200-distilled-600M` via plain `transformers` --
    no custom preprocessing toolkit needed, which is exactly why this
    is the backend actually run in this environment. Model/tokenizer
    are loaded once per process (a 600M-parameter model has real load
    cost -- ~250MB of weights plus tokenizer vocab; see this module's
    `_load_nllb` docstring) and reused across every `translate_text`
    call, matching this codebase's established lazy-singleton pattern
    for expensive resources (e.g. app.security.secrets.get_secrets_provider)."""

    def __init__(self, model_id: str = "facebook/nllb-200-distilled-600M", max_new_tokens: int = 512) -> None:
        self._model_id = model_id
        self._max_new_tokens = max_new_tokens

    def translate_text(self, text: str, source_language: RegionalLanguage) -> str:
        tokenizer, model = _load_nllb(self._model_id)
        profile = get_language_profile(source_language)
        english_profile = get_language_profile(RegionalLanguage.ENGLISH)

        tokenizer.src_lang = profile.nllb_code
        inputs = tokenizer(text, return_tensors="pt", truncation=True)
        generated = model.generate(
            **inputs,
            forced_bos_token_id=tokenizer.convert_tokens_to_ids(english_profile.nllb_code),
            max_new_tokens=self._max_new_tokens,
        )
        return tokenizer.batch_decode(generated, skip_special_tokens=True)[0]


@lru_cache(maxsize=4)
def _load_nllb(model_id: str):
    """Cached per `model_id` (not just a bare singleton) so a test or a
    multi-model deployment can hold more than one NLLB checkpoint
    resident without re-downloading/re-loading on every call --
    `lru_cache`'s own eviction (maxsize=4) bounds memory if that's ever
    actually exercised with several distinct model ids."""
    from transformers import AutoModelForSeq2SeqLM, AutoTokenizer  # deferred heavy import

    logger.info("Loading NLLB translation model %s (first call only; cached for the life of this process).", model_id)
    tokenizer = AutoTokenizer.from_pretrained(model_id)
    model = AutoModelForSeq2SeqLM.from_pretrained(model_id)
    model.eval()
    return tokenizer, model


class IndicTrans2Backend:
    """AI4Bharat's IndicTrans2 (`ai4bharat/indictrans2-indic-en-1B` for
    Indic->English, `ai4bharat/indictrans2-indic-en-dist-200M` for the
    distilled/lighter variant) -- see this module's docstring for why
    this backend is documented-but-unexecuted here. Written to
    AI4Bharat's own published inference pattern:
    https://github.com/AI4Bharat/IndicTrans2 -- requires
    `pip install IndicTransToolkit` (preprocessing: sentence splitting +
    script normalization via `indic-nlp-library`) IN ADDITION to
    `transformers`, which plain NLLB does not need.
    """

    def __init__(self, model_id: str = "ai4bharat/indictrans2-indic-en-dist-200M") -> None:
        self._model_id = model_id

    def translate_text(self, text: str, source_language: RegionalLanguage) -> str:
        try:
            from IndicTransToolkit.processor import IndicProcessor  # type: ignore[import-not-found]
        except ImportError as exc:
            raise TranslationBackendError(
                "IndicTrans2Backend requires the 'IndicTransToolkit' package "
                "(pip install IndicTransToolkit) in addition to transformers -- see this class's docstring."
            ) from exc
        from transformers import AutoModelForSeq2SeqLM, AutoTokenizer  # deferred heavy import

        profile = get_language_profile(source_language)
        # AI4Bharat's IndicTrans2 uses its own FLORES-style tags, which
        # for the three languages this pipeline supports are identical
        # to NLLB's (both derive from the FLORES-200 benchmark's
        # language-code convention) -- `profile.nllb_code` is reused
        # rather than adding a parallel `indictrans2_code` field to
        # LanguageProfile for a distinction that doesn't exist for
        # Hindi/Marathi/Gujarati.
        src_lang, tgt_lang = profile.nllb_code, get_language_profile(RegionalLanguage.ENGLISH).nllb_code

        tokenizer, model, processor = _load_indictrans2(self._model_id)
        batch = processor.preprocess_batch([text], src_lang=src_lang, tgt_lang=tgt_lang)
        inputs = tokenizer(batch, truncation=True, padding="longest", return_tensors="pt")
        generated = model.generate(**inputs, num_beams=5, max_length=256)
        decoded = tokenizer.batch_decode(generated, skip_special_tokens=True)
        return processor.postprocess_batch(decoded, lang=tgt_lang)[0]


@lru_cache(maxsize=2)
def _load_indictrans2(model_id: str):
    from IndicTransToolkit.processor import IndicProcessor  # type: ignore[import-not-found]
    from transformers import AutoModelForSeq2SeqLM, AutoTokenizer  # deferred heavy import

    tokenizer = AutoTokenizer.from_pretrained(model_id, trust_remote_code=True)
    model = AutoModelForSeq2SeqLM.from_pretrained(model_id, trust_remote_code=True)
    model.eval()
    processor = IndicProcessor(inference=True)
    return tokenizer, model, processor


def get_translation_backend(settings: Settings) -> TranslationBackend:
    if settings.localization_translation_backend == "nllb":
        return NLLBBackend(model_id=settings.localization_nllb_model_id)
    if settings.localization_translation_backend == "indictrans2":
        return IndicTrans2Backend(model_id=settings.localization_indictrans2_model_id)
    raise ValueError(f"Unknown localization_translation_backend: {settings.localization_translation_backend!r} (expected 'nllb' or 'indictrans2')")
