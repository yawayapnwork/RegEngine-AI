"""Typed exceptions for the parsing pipeline.

Kept distinct from generic exceptions so the API layer can map each one to
an appropriate HTTP status instead of leaking 500s for client-caused errors.
"""
from __future__ import annotations


class ParsingError(Exception):
    """Base class for all parsing-pipeline failures."""


class UnsupportedFileError(ParsingError):
    """Raised when the upload is not a parsable PDF (bad magic bytes, empty, etc.)."""


class ExtractionBackendError(ParsingError):
    """Raised when both the primary and fallback extraction backends fail."""


class ParseTimeoutError(ParsingError):
    """Raised when extraction exceeds the configured timeout."""


class ChunkingError(ParsingError):
    """Raised when semantic chunking cannot produce a valid chunk set."""


class EmbeddingError(ParsingError):
    """Raised when the embedding backend fails to vectorize chunks."""


class IndexingError(ParsingError):
    """Raised when Qdrant upsert fails after retries."""
