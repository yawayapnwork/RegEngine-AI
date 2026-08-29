"""Scenario 3 fault injection: malformed/truncated SEBI PDF feeds fed
into `app.parsing.extractor.extract_pdf`.

Two distinct failure shapes, since they exercise different layers of
`extract_pdf`:

  * `empty_bytes()` / `not_a_pdf_bytes()` -- fail `_validate_pdf_bytes`'s
    magic-byte check before any extraction backend even runs. Pure,
    dependency-light, and exercised directly against the real function
    (no mocking needed).
  * `truncated_pdf_bytes()` -- passes the magic-byte check (has a real
    `%PDF-` header) but is corrupted/cut off deeper in the file, the way
    a dropped RSS download or a partial S3 multipart upload would
    actually produce a bad file. `unstructured`/`tika` are not installed
    in this sandbox (they're heavy, deferred-imported dependencies --
    see app.parsing.extractor's module docstring), so exercising this
    path needs `app.parsing.extractor._partition_with_unstructured` /
    `_partition_with_tika` monkeypatched to raise the way those
    libraries actually do on a truncated stream (a parser exception, not
    a clean empty result) -- see chaos/monkey/validators.py's Scenario 3
    check for exactly how.
"""
from __future__ import annotations

_PDF_HEADER = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n"


def empty_bytes() -> bytes:
    return b""


def not_a_pdf_bytes() -> bytes:
    return b"This is a plain text file pretending to be a SEBI circular, not a PDF at all."


def truncated_pdf_bytes() -> bytes:
    """A real `%PDF-` header followed by a plausible object stream that
    is cut off mid-object -- passes the magic-byte sniff but is not a
    parsable PDF, standing in for a feed download that dropped partway
    through (the literal "delayed/truncated feed" failure mode
    Requirement 1 names)."""
    return _PDF_HEADER + (
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /Contents 4 0 R"
        # deliberately truncated here -- no closing '>>', no 'endobj', no
        # xref table, no '%%EOF'
    )
