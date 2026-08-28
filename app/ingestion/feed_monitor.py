"""Discovers candidate circular/notification links from SEBI's RSS feeds and
HTML listing pages. Discovery only — no change detection or download here."""
from __future__ import annotations

import datetime as dt
import logging
import re
from urllib.parse import urljoin

import feedparser
from bs4 import BeautifulSoup
from dateutil import parser as date_parser

from app.config import Settings
from app.ingestion.exceptions import SourceFetchError
from app.ingestion.http_client import SebiHttpClient
from app.ingestion.models import DiscoveredDocument, SourceKind

logger = logging.getLogger(__name__)

_CIRCULAR_NUMBER_RE = re.compile(r"\b([A-Z]{2,}/[A-Z0-9/-]+/\d{4}/\d+)\b")
_PDF_HREF_RE = re.compile(r"\.pdf(?:\?.*)?$", re.IGNORECASE)


def _extract_circular_number(text: str) -> str | None:
    match = _CIRCULAR_NUMBER_RE.search(text)
    return match.group(1) if match else None


def _parse_datetime(raw: str | None) -> dt.datetime | None:
    if not raw:
        return None
    try:
        parsed = date_parser.parse(raw)
    except (ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


async def poll_rss_feed(client: SebiHttpClient, feed_url: str) -> list[DiscoveredDocument]:
    """Fetch and parse one RSS/Atom feed into discovered documents."""
    try:
        response = await client.get(feed_url)
    except SourceFetchError:
        logger.warning("RSS feed unreachable, skipping this cycle: %s", feed_url)
        return []

    parsed = feedparser.parse(response.content)
    if parsed.bozo and not parsed.entries:
        logger.warning("RSS feed at %s parsed with errors and yielded no entries: %s", feed_url, parsed.bozo_exception)
        return []

    documents: list[DiscoveredDocument] = []
    for entry in parsed.entries:
        link = entry.get("link")
        title = entry.get("title", "").strip()
        if not link or not title:
            continue
        published_raw = entry.get("published") or entry.get("updated")
        documents.append(
            DiscoveredDocument(
                source_url=link,
                source_kind=SourceKind.RSS,
                title=title,
                published_at=_parse_datetime(published_raw),
                circular_number=_extract_circular_number(title) or _extract_circular_number(link),
            )
        )
    logger.info("RSS feed %s yielded %d entries", feed_url, len(documents))
    return documents


async def poll_html_listing(client: SebiHttpClient, listing_url: str) -> list[DiscoveredDocument]:
    """Fallback/supplement: scrape a SEBI HTML listing page directly for PDF
    links. Used because the RSS feed can lag or omit master-circular
    consolidation updates that don't fire a fresh "new circular" RSS entry.
    """
    try:
        response = await client.get(listing_url)
    except SourceFetchError:
        logger.warning("HTML listing unreachable, skipping this cycle: %s", listing_url)
        return []

    soup = BeautifulSoup(response.text, "lxml")
    documents: list[DiscoveredDocument] = []

    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if not _PDF_HREF_RE.search(href):
            continue
        title = anchor.get_text(strip=True) or href.rsplit("/", 1)[-1]
        absolute_url = urljoin(listing_url, href)

        # SEBI listing rows commonly place the publish date in a sibling
        # <td>; best-effort only, change detection below does not depend on it.
        published_at = None
        row = anchor.find_parent("tr")
        if row is not None:
            row_text = row.get_text(" ", strip=True)
            date_match = re.search(r"\b(\d{1,2}\s+\w+\s+\d{4})\b", row_text)
            if date_match:
                published_at = _parse_datetime(date_match.group(1))

        documents.append(
            DiscoveredDocument(
                source_url=absolute_url,
                source_kind=SourceKind.HTML_LISTING,
                title=title,
                published_at=published_at,
                circular_number=_extract_circular_number(title) or _extract_circular_number(absolute_url),
            )
        )
    logger.info("HTML listing %s yielded %d PDF links", listing_url, len(documents))
    return documents


async def discover_all(client: SebiHttpClient, settings: Settings) -> list[DiscoveredDocument]:
    """Poll every configured RSS feed and HTML listing page, merged and
    de-duplicated by source URL (RSS entries win on conflict — they carry
    more reliable publish timestamps)."""
    by_url: dict[str, DiscoveredDocument] = {}

    for listing_url in settings.sebi_listing_page_urls:
        for doc in await poll_html_listing(client, listing_url):
            by_url[doc.source_url] = doc

    for feed_url in settings.sebi_rss_feed_urls:
        for doc in await poll_rss_feed(client, feed_url):
            by_url[doc.source_url] = doc  # RSS overwrites HTML-listing entry for the same URL

    return list(by_url.values())
