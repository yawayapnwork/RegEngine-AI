"""Typed exceptions for the ingestion pipeline."""
from __future__ import annotations


class IngestionError(Exception):
    """Base class for all ingestion-pipeline failures."""


class SourceFetchError(IngestionError):
    """Raised when a feed or listing page cannot be fetched after retries."""


class DocumentDownloadError(IngestionError):
    """Raised when a discovered PDF cannot be downloaded after retries."""


class RobotsDisallowedError(IngestionError):
    """Raised when robots.txt disallows fetching a target URL."""
