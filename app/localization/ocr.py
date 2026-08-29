"""Requirement 1's OCR half: layout-aware text extraction from a
regional-language PDF page image, via PaddleOCR or Tesseract.

Backend routing (see app.localization.languages' module docstring for
the full rationale): Hindi and Marathi share the Devanagari script, for
which PaddleOCR ships one combined "devanagari" recognition model, so
they route there by default; PaddleOCR has never shipped a Gujarati-
script model, so Gujarati routes to Tesseract's `guj.traineddata`
unconditionally. Either backend can be forced via `prefer_backend`.

Neither PaddleOCR nor a Tesseract binary is installed in this
environment (no OCR binary/model download was attempted here -- see
this module's parity with app.localization.translation's
IndicTrans2Backend on the "correct code for infrastructure this
environment doesn't have" distinction). What IS verified for real here:
`pytesseract` itself (the Python binding) is installed, and this
module's Tesseract path correctly raises a typed, actionable
`OCRBackendError` -- not an unhandled `TesseractNotFoundError` or a
silent empty result -- when the underlying binary is missing (see
tests/test_localization.py's `TestTesseractBackendMissingBinary`).
"""
from __future__ import annotations

import logging
from functools import lru_cache

from pydantic import BaseModel, Field

from app.config import Settings
from app.localization.languages import OCRBackendChoice, RegionalLanguage, get_language_profile

logger = logging.getLogger(__name__)


class OCRBackendError(RuntimeError):
    pass


class OCRTextBlock(BaseModel):
    text: str
    confidence: float = Field(..., description="0.0-1.0, normalized across backends (Tesseract reports 0-100; PaddleOCR reports 0.0-1.0 natively).")
    bbox: tuple[float, float, float, float] | None = Field(None, description="(x0, y0, x1, y1) in the source image's pixel coordinates.")


class OCRResult(BaseModel):
    language: RegionalLanguage
    backend_used: OCRBackendChoice
    blocks: list[OCRTextBlock]

    @property
    def full_text(self) -> str:
        return "\n".join(block.text for block in self.blocks if block.text.strip())


def select_ocr_backend(language: RegionalLanguage, prefer_backend: OCRBackendChoice | None = None) -> OCRBackendChoice:
    profile = get_language_profile(language)
    if profile.paddleocr_lang_code is None:
        return OCRBackendChoice.TESSERACT  # no PaddleOCR model exists for this script -- not a preference, a hard constraint
    return prefer_backend or profile.preferred_ocr_backend


@lru_cache(maxsize=4)
def _load_paddleocr(paddleocr_lang_code: str):
    from paddleocr import PaddleOCR  # deferred heavy import

    logger.info("Loading PaddleOCR (lang=%s) -- first call only; cached for the life of this process.", paddleocr_lang_code)
    return PaddleOCR(lang=paddleocr_lang_code, use_angle_cls=True, show_log=False)


def extract_text_paddleocr(image_path: str, language: RegionalLanguage) -> OCRResult:
    profile = get_language_profile(language)
    if profile.paddleocr_lang_code is None:
        raise OCRBackendError(f"PaddleOCR has no recognition model for {profile.display_name} ({profile.script} script) -- use Tesseract for this language.")

    try:
        ocr = _load_paddleocr(profile.paddleocr_lang_code)
        raw_result = ocr.ocr(image_path, cls=True)
    except ImportError as exc:
        raise OCRBackendError("PaddleOCR is not installed (pip install paddleocr paddlepaddle).") from exc
    except Exception as exc:  # noqa: BLE001 - PaddleOCR raises a variety of backend-specific errors (model download failure, corrupt image, etc.); all become one typed error for this pipeline's callers
        raise OCRBackendError(f"PaddleOCR failed on {image_path!r}: {exc}") from exc

    blocks: list[OCRTextBlock] = []
    for line in (raw_result[0] if raw_result and raw_result[0] else []):
        bbox_points, (text, confidence) = line
        xs = [p[0] for p in bbox_points]
        ys = [p[1] for p in bbox_points]
        blocks.append(OCRTextBlock(text=text, confidence=float(confidence), bbox=(min(xs), min(ys), max(xs), max(ys))))

    return OCRResult(language=language, backend_used=OCRBackendChoice.PADDLEOCR, blocks=blocks)


def extract_text_tesseract(image_path: str, language: RegionalLanguage, *, tesseract_cmd: str | None = None) -> OCRResult:
    try:
        import pytesseract
        from PIL import Image
    except ImportError as exc:
        raise OCRBackendError("pytesseract/Pillow are not installed (pip install pytesseract Pillow).") from exc

    if tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = tesseract_cmd

    profile = get_language_profile(language)
    # pytesseract.TesseractError (raised when the binary IS found but
    # rejects the language pack / image) is deliberately NOT caught here
    # -- that failure needs a different fix (install the language
    # traineddata) than TesseractNotFoundError does, and a shared except
    # clause would blur the two into one misleading message; only
    # TesseractNotFoundError gets this module's own diagnostic.
    try:
        image = Image.open(image_path)
        data = pytesseract.image_to_data(image, lang=profile.tesseract_lang_code, output_type=pytesseract.Output.DICT)
    except pytesseract.TesseractNotFoundError as exc:
        raise OCRBackendError(
            "The 'tesseract' binary was not found on PATH (installing the pytesseract PYTHON PACKAGE alone is not "
            "enough -- Tesseract OCR itself must also be installed separately: apt-get install tesseract-ocr "
            f"tesseract-ocr-{profile.tesseract_lang_code} / choco install tesseract, or set "
            "settings.localization_tesseract_cmd_path to its binary's full path)."
        ) from exc

    blocks: list[OCRTextBlock] = []
    for i, text in enumerate(data["text"]):
        text = text.strip()
        if not text:
            continue
        confidence = float(data["conf"][i])
        if confidence < 0:  # Tesseract uses -1 for a non-text detection region
            continue
        x, y, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
        blocks.append(OCRTextBlock(text=text, confidence=confidence / 100.0, bbox=(float(x), float(y), float(x + w), float(y + h))))

    return OCRResult(language=language, backend_used=OCRBackendChoice.TESSERACT, blocks=blocks)


def extract_regional_text(
    image_path: str,
    language: RegionalLanguage,
    settings: Settings,
    *,
    prefer_backend: OCRBackendChoice | None = None,
) -> OCRResult:
    """The entrypoint app.localization.pipeline calls. Falls back from
    PaddleOCR to Tesseract on any `OCRBackendError` (e.g. PaddleOCR not
    installed, or its model download failing) -- never the reverse
    (Tesseract failing does not fall back to PaddleOCR), since Tesseract
    is this pipeline's universal baseline (it has a language pack for
    all three supported regional languages; PaddleOCR does not) and a
    Tesseract failure therefore means the destination is genuinely
    unavailable, not that a better option exists to fall back to."""
    backend = select_ocr_backend(language, prefer_backend)

    if backend == OCRBackendChoice.PADDLEOCR:
        try:
            return extract_text_paddleocr(image_path, language)
        except OCRBackendError as exc:
            logger.warning("PaddleOCR failed for %s (%s); falling back to Tesseract.", language.value, exc)

    return extract_text_tesseract(image_path, language, tesseract_cmd=settings.localization_tesseract_cmd_path)
