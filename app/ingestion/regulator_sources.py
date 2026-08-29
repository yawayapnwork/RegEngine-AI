"""Maps each supported regulator (app.regulatory.taxonomy.Regulator) to
its configured RSS feeds and HTML listing pages, so
app.ingestion.feed_monitor.discover_all can poll every regulator's
sources through the exact same SebiHttpClient/poll_rss_feed/
poll_html_listing machinery -- that machinery (retry/backoff, User-Agent
rotation, robots.txt compliance, rate limiting) is generic polite-HTTP-
client behavior, not actually SEBI-specific, despite its name.

This is the ingestion half of the "Routing Layer" requirement: it
decides WHICH regulator a discovered document belongs to (deterministically,
from which feed/listing page produced it), so downstream stages
(app.parsing.extractor's source_tag, app.agents.crew's regulator-aware
agent backstory, app.compiler's regulator-namespaced Rego package) never
have to guess.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings
from app.regulatory.taxonomy import Regulator


@dataclass(frozen=True)
class RegulatorSourceConfig:
    rss_feed_urls: list[str]
    listing_page_urls: list[str]


def resolve_regulator_sources(settings: Settings) -> dict[Regulator, RegulatorSourceConfig]:
    """Built from settings on every call (not module-level) so tests and
    ops can override a regulator's feed URLs via `Settings` construction
    without needing a process restart or a cache-invalidation path."""
    return {
        Regulator.SEBI: RegulatorSourceConfig(
            rss_feed_urls=settings.sebi_rss_feed_urls,
            listing_page_urls=settings.sebi_listing_page_urls,
        ),
        Regulator.RBI: RegulatorSourceConfig(
            rss_feed_urls=settings.rbi_rss_feed_urls,
            listing_page_urls=settings.rbi_listing_page_urls,
        ),
        Regulator.IRDAI: RegulatorSourceConfig(
            rss_feed_urls=settings.irdai_rss_feed_urls,
            listing_page_urls=settings.irdai_listing_page_urls,
        ),
        Regulator.PFRDA: RegulatorSourceConfig(
            rss_feed_urls=settings.pfrda_rss_feed_urls,
            listing_page_urls=settings.pfrda_listing_page_urls,
        ),
    }


def count_configured_sources(settings: Settings) -> int:
    """Total feed+listing URL count across every regulator -- replaces
    the old SEBI-only `len(settings.sebi_rss_feed_urls) +
    len(settings.sebi_listing_page_urls)` used for
    `IngestionRunResult.sources_polled` (app.ingestion.tasks)."""
    return sum(
        len(cfg.rss_feed_urls) + len(cfg.listing_page_urls)
        for cfg in resolve_regulator_sources(settings).values()
    )
