"""Cryptographic hashing utilities for clause-level traceability.

Every extracted clause block gets a SHA-256 digest computed over a
normalized, canonical representation of its content and identifying
metadata. This lets downstream systems (audit trails, dedup, diffing
against a re-issued circular) verify that a chunk's text has not been
altered without needing to re-fetch the source PDF.
"""
from __future__ import annotations

import hashlib
import unicodedata


def _canonicalize(text: str) -> str:
    """Normalize unicode and collapse whitespace so semantically identical
    text produces an identical hash regardless of incidental PDF extraction
    artifacts (double spaces, non-breaking spaces, mixed line endings)."""
    normalized = unicodedata.normalize("NFKC", text)
    return " ".join(normalized.split())


def sha256_of_text(text: str) -> str:
    canonical = _canonicalize(text)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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
