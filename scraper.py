"""
Zero-cost data acquisition: SEC EDGAR filings + Google News RSS.

All sources are public RSS/Atom feeds. No API keys required.
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import quote_plus

import feedparser
import requests

# SEC requires a descriptive User-Agent with contact info on every request.
SEC_USER_AGENT = "ResearchBot admin@investor.com"
SEC_HEADERS = {
    "User-Agent": SEC_USER_AGENT,
    "Accept-Encoding": "gzip, deflate",
    "Host": "www.sec.gov",
}

GOOGLE_NEWS_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

TARGET_FORMS = ("10-K", "10-Q", "8-K")
REQUEST_TIMEOUT = 20


def _clean(text: str | None, limit: int = 1200) -> str:
    if not text:
        return ""
    collapsed = " ".join(text.split())
    return collapsed[:limit]


def _is_us_ticker(ticker: str) -> bool:
    """EDGAR only covers US-listed issuers; skip foreign tickers like 6954.T."""
    return ticker.isalpha()


def fetch_sec_filings(ticker: str, max_items: int = 6) -> list[dict[str, Any]]:
    """
    Fetch recent 10-K / 10-Q / 8-K filings for a ticker from EDGAR's Atom feed.

    Returns a list of {title, summary, url, source} dicts.
    """
    if not _is_us_ticker(ticker):
        print(f"[scraper] {ticker}: non-US ticker, skipping EDGAR.")
        return []

    items: list[dict[str, Any]] = []
    for form in TARGET_FORMS:
        url = (
            "https://www.sec.gov/cgi-bin/browse-edgar"
            f"?action=getcompany&ticker={quote_plus(ticker)}"
            f"&type={quote_plus(form)}&dateb=&owner=include&count=10&output=atom"
        )
        try:
            resp = requests.get(url, headers=SEC_HEADERS, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
        except requests.RequestException as exc:
            print(f"[scraper] {ticker} {form}: EDGAR request failed ({exc}).")
            continue

        feed = feedparser.parse(resp.content)
        for entry in feed.entries[:max_items]:
            title = entry.get("title", f"{form} filing")
            link = entry.get("link", "")
            summary = _clean(entry.get("summary") or entry.get("title"))
            items.append(
                {
                    "title": f"SEC {form}: {title}",
                    "summary": summary,
                    "url": link,
                    "source": "SEC EDGAR",
                }
            )
        # Stay well under SEC's 10 requests/second fair-access limit.
        time.sleep(0.4)

    return items


def fetch_google_news(company_name: str, max_items: int = 12) -> list[dict[str, Any]]:
    """
    Fetch Google News RSS results tuned to CapEx / supply-chain intelligence.
    """
    query = f"{company_name} capital expenditure supply chain bottleneck"
    url = (
        "https://news.google.com/rss/search"
        f"?q={quote_plus(query)}&hl=en-US&gl=US&ceid=US:en"
    )
    try:
        resp = requests.get(
            url, headers=GOOGLE_NEWS_HEADERS, timeout=REQUEST_TIMEOUT
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"[scraper] {company_name}: Google News request failed ({exc}).")
        return []

    feed = feedparser.parse(resp.content)
    items: list[dict[str, Any]] = []
    for entry in feed.entries[:max_items]:
        title = entry.get("title", "")
        link = entry.get("link", "")
        source = ""
        if entry.get("source") and isinstance(entry.source, dict):
            source = entry.source.get("title", "")
        summary = _clean(entry.get("summary") or title)
        items.append(
            {
                "title": title,
                "summary": summary,
                "url": link,
                "source": f"Google News{f' / {source}' if source else ''}",
            }
        )
    return items


def gather_raw_items(
    ticker: str, company_name: str, skip_seen: bool = True
) -> list[dict[str, Any]]:
    """
    Aggregate SEC + Google News raw text items for one company.

    Each item: {title, summary, url, source}.

    When ``skip_seen`` is True, URLs already recorded in Supabase's 'seen_urls'
    table are dropped so they are never sent through Gemini a second time. If the
    db layer is unavailable (e.g. running the scraper standalone) the filter is
    silently skipped.
    """
    items: list[dict[str, Any]] = []
    items.extend(fetch_sec_filings(ticker))
    items.extend(fetch_google_news(company_name))

    # De-duplicate on URL within this batch while preserving order.
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for item in items:
        key = item.get("url") or item.get("title", "")
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)

    collected = len(deduped)

    if skip_seen:
        try:
            from db import filter_unseen_items

            deduped = filter_unseen_items(deduped)
        except Exception as exc:  # ImportError, missing creds, network, ...
            print(f"[scraper] seen-URL filter unavailable ({exc}); grading all.")

    print(
        f"[scraper] {company_name} ({ticker}): "
        f"{collected} collected, {len(deduped)} new to grade."
    )
    return deduped
