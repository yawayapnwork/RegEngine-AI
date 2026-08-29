"""Cross-lingual translation parity checker for SEBI circulars released
concurrently in English and Hindi.

Distinct from `app.localization`: that package validates a MACHINE
translation this platform itself produces from a single regional-
language source circular (source text -> NLLB/IndicTrans2 -> verify
against its own source). This package instead compares TWO
independently, human-authored SEBI documents -- the English circular
and its concurrently-released Hindi counterpart -- clause by clause,
before either is compiled into policy. It reuses `app.localization`'s
proven per-pair verification primitives
(`app.localization.verification.CrossLingualVerifier`,
`app.localization.numeric_precision.check_numeric_precision`) rather
than reimplementing cross-lingual similarity or numeric-token
extraction; what this package adds is clause ALIGNMENT across two
separate documents, missing-clause detection, side-by-side diff
rendering, and a compliance-officer review queue -- concerns neither
`app.localization` nor `app.diffing` (version-over-time diffing of a
single language's Master Circular index) already cover.

Gated behind `settings.translation_parity_enabled` (default False).
"""
