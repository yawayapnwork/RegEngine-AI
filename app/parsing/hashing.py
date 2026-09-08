"""Cryptographic hashing utilities for document and clause-level traceability.

RegEngine separates document-level and clause-level identity across distinct,
unambiguous cryptographic hashes:

1. `source_document_sha256`: SHA-256 computed over the ORIGINAL uploaded raw bytes
   (e.g. PDF container) BEFORE any parsing, decoding, or text transformation.
   This provides an immutable cryptographic fingerprint of the physical uploaded artifact.
   Filenames are never used as a cryptographic identity.

2. `extracted_text_sha256` (historically `raw_text_digest`): SHA-256 computed over
   the canonicalized, extracted textual content across all chunks/elements.
   This identifies the document's legal text independently of container/encoding artifacts.
   Two different PDF files (e.g. with differing metadata, digital signatures, or comments)
   that contain identical legal text will have different `source_document_sha256` values
   but identical `extracted_text_sha256` values.

3. `clause.sha256`: SHA-256 scoped to a specific clause block
   (circular_number + clause_number + canonicalized clause text).
"""
from __future__ import annotations

import hashlib
import unicodedata


def sha256_of_bytes(data: bytes) -> str:
    """Calculates the immutable cryptographic SHA-256 hex digest over raw binary
    content (e.g. original uploaded PDF bytes).

    MUST be computed directly on the source byte stream before any extraction,
    transcoding, or text parsing. Never uses filenames as cryptographic identity.
    """
    return hashlib.sha256(data).hexdigest()


def _canonicalize(text: str) -> str:
    """Normalize unicode and collapse whitespace so semantically identical
    text produces an identical hash regardless of incidental PDF extraction
    artifacts (double spaces, non-breaking spaces, mixed line endings)."""
    normalized = unicodedata.normalize("NFKC", text)
    return " ".join(normalized.split())


def sha256_of_text(text: str) -> str:
    """Calculates the SHA-256 digest over normalized text."""
    canonical = _canonicalize(text)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def sha256_of_extracted_text(text: str) -> str:
    """Calculates the SHA-256 digest of normalized extracted text from a document.

    Alias/wrapper for sha256_of_text with explicit semantic naming for
    document-level extracted text digests (`extracted_text_sha256`).
    """
    return sha256_of_text(text)


def sha256_of_clause(
    *,
    circular_number: str | None,
    clause_number: str | None,
    text: str,
) -> str:
    """Digest scoped to the clause's identity (circular + clause number +
    text), so the same clause text appearing under two different circulars
    (e.g. a re-issue) hashes differently — required for traceability."""
    parts = [circular_number or "", clause_number or "", _canonicalize(text)]
    payload = "\x1f".join(parts)  # unit-separator avoids field-collision
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

