"""Regional-Indian-language support for the ingestion engine
(Requirement: process financial notices, gazette circulars, and
investor disclosures published in Hindi, Marathi, Gujarati -- alongside
English -- without changing anything downstream of ingestion).

Pipeline (app.localization.pipeline.process_regional_document):

    scanned regional PDF page image
          |  app.localization.ocr (PaddleOCR / Tesseract, layout-aware)
          v
    regional-script raw text  ----------------------------+
          |  app.localization.translation (IndicTrans2 / NLLB)   |
          v                                                       |
    English translation                                           |
          |  app.localization.verification (cross-lingual         |
          |  semantic similarity + numeric-precision check) <-----+
          v
    verified English ClauseChunk, `extra` carries source_language,
    original_text, and the verification result for audit -- fed into
    the EXISTING app.agents extraction/audit pipeline completely
    unchanged. Regional-language support is therefore an ADDITION to
    ingestion, never a fork of extraction/compilation/execution.
"""
