"""Unit tests for the SEBI ingestion pipeline: RSS/HTML discovery parsing
and change-detection classification. HTTP and Redis are stubbed so these
run offline with no external services."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.ingestion.change_detector import SeenDocumentStore
from app.ingestion.feed_monitor import _extract_circular_number, poll_html_listing, poll_rss_feed
from app.ingestion.models import ChangeKind, DiscoveredDocument, SourceKind

_SAMPLE_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>SEBI Circulars</title>
    <item>
      <title>Circular on Margin Trading Framework SEBI/HO/MRD/DP/CIR/P/2026/45</title>
      <link>https://www.sebi.gov.in/legal/circulars/margin-trading-2026-45.pdf</link>
      <pubDate>Fri, 21 Aug 2026 10:00:00 +0530</pubDate>
    </item>
    <item>
      <title>Master Circular for Stock Brokers</title>
      <link>https://www.sebi.gov.in/legal/master-circulars/stock-brokers.pdf</link>
      <pubDate>Thu, 20 Aug 2026 09:00:00 +0530</pubDate>
    </item>
  </channel>
</rss>
"""

_SAMPLE_HTML = b"""
<html><body>
<table>
  <tr><td>21 Aug 2026</td><td><a href="/legal/circulars/margin-trading-2026-45.pdf">Margin Trading Framework</a></td></tr>
  <tr><td>15 Aug 2026</td><td><a href="/legal/circulars/old-notice.pdf">Old Notice</a></td></tr>
  <tr><td>-</td><td><a href="/legal/circulars/not-a-doc.html">Not a PDF</a></td></tr>
</table>
</body></html>
"""


def _fake_response(content: bytes, text: str | None = None):
    return SimpleNamespace(content=content, text=text if text is not None else content.decode())


class _FakeClient:
    def __init__(self, payload: bytes, text: str | None = None) -> None:
        self._payload = payload
        self._text = text
        self.get = AsyncMock(side_effect=self._get)

    async def _get(self, url: str):
        return _fake_response(self._payload, self._text)


def test_extract_circular_number_from_title():
    assert _extract_circular_number("Circular SEBI/HO/MRD/DP/CIR/P/2026/45 on margin") == "SEBI/HO/MRD/DP/CIR/P/2026/45"


def test_extract_circular_number_absent():
    assert _extract_circular_number("A circular with no reference number") is None


@pytest.mark.asyncio
async def test_poll_rss_feed_parses_entries():
    client = _FakeClient(_SAMPLE_RSS)
    documents = await poll_rss_feed(client, "https://www.sebi.gov.in/sebirss.xml")

    assert len(documents) == 2
    assert documents[0].source_kind == SourceKind.RSS
    assert documents[0].source_url.endswith("margin-trading-2026-45.pdf")
    assert documents[0].circular_number == "SEBI/HO/MRD/DP/CIR/P/2026/45"
    assert documents[0].published_at is not None


@pytest.mark.asyncio
async def test_poll_html_listing_extracts_pdf_links_only():
    client = _FakeClient(_SAMPLE_HTML, text=_SAMPLE_HTML.decode())
    documents = await poll_html_listing(client, "https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListing=yes")

    urls = {doc.source_url for doc in documents}
    assert any(u.endswith("margin-trading-2026-45.pdf") for u in urls)
    assert any(u.endswith("old-notice.pdf") for u in urls)
    assert not any(u.endswith("not-a-doc.html") for u in urls)
    assert all(doc.source_kind == SourceKind.HTML_LISTING for doc in documents)


class _FakeRedis:
    """Minimal async stand-in for the redis.asyncio.Redis hash operations
    SeenDocumentStore uses, backed by an in-memory dict."""

    def __init__(self) -> None:
        self._hash: dict[str, str] = {}

    async def hget(self, _key: str, field: str) -> str | None:
        return self._hash.get(field)

    async def hset(self, _key: str, field: str, value: str) -> None:
        self._hash[field] = value

    async def hkeys(self, _key: str) -> list[str]:
        return list(self._hash.keys())


@pytest.mark.asyncio
async def test_change_detector_classifies_new_then_amended_then_unchanged():
    store = SeenDocumentStore(_FakeRedis(), key_prefix="test:ingestion")
    doc = DiscoveredDocument(
        source_url="https://www.sebi.gov.in/legal/circulars/margin-trading-2026-45.pdf",
        source_kind=SourceKind.RSS,
        title="Margin Trading Framework",
    )

    change_kind, content_hash_v1 = await store.classify(doc, b"version one content")
    assert change_kind == ChangeKind.NEW_DOCUMENT
    await store.record(doc.source_url, content_hash_v1)

    change_kind, content_hash_v1_again = await store.classify(doc, b"version one content")
    assert change_kind == ChangeKind.UNCHANGED
    assert content_hash_v1_again == content_hash_v1

    change_kind, content_hash_v2 = await store.classify(doc, b"version two content, amended clause")
    assert change_kind == ChangeKind.CONTENT_AMENDED
    assert content_hash_v2 != content_hash_v1
