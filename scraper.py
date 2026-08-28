"""
Zero-cost data acquisition with full article / filing text.

Sources (no API keys):
  * Google News RSS  -> recent headlines; redirect URLs are resolved to the
                        real publisher link, then the article body is extracted
  * SEC EDGAR        -> recent 8-K / 10-K / 10-Q primary-document text
                        (data.sec.gov submissions JSON)

Bodies are extracted with trafilatura so the analyzer grades real content,
not just a headline.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, NamedTuple
from urllib.parse import quote_plus, urlparse

import feedparser
import requests
import trafilatura


class ScrapeResult(NamedTuple):
    """Outcome of scraping one company."""

    items: list[dict[str, Any]]  # items to grade (post seen-URL filter)
    collected: int               # unique items found before the seen-URL filter


BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
BROWSER_HEADERS = {"User-Agent": BROWSER_UA}
# SEC requires a descriptive User-Agent with contact info on every request.
SEC_HEADERS = {
    "User-Agent": "ResearchBot admin@investor.com",
    "Accept-Encoding": "gzip, deflate",
}

REQUEST_TIMEOUT = 20
ARTICLE_TIMEOUT = 15
ARTICLE_TEXT_CHARS = 3500
MIN_BODY_CHARS = 220

TARGET_FORMS = ("8-K", "10-K", "10-Q")
_CIK_MAP: dict[str, str] | None = None


# --------------------------------------------------------------------- helpers

def _clean(text: str | None, limit: int = ARTICLE_TEXT_CHARS) -> str:
    if not text:
        return ""
    return " ".join(text.split())[:limit]


def _is_us_ticker(ticker: str) -> bool:
    """EDGAR only covers US-listed issuers; skip foreign tickers like 6954.T."""
    return "." not in ticker and ticker.isalpha()


def extract_article_text(url: str, headers: dict[str, str] | None = None) -> str:
    """Fetch a URL and return its main body text ('' on any failure)."""
    try:
        resp = requests.get(
            url, headers=headers or BROWSER_HEADERS, timeout=ARTICLE_TIMEOUT
        )
        resp.raise_for_status()
        text = trafilatura.extract(
            resp.text,
            include_comments=False,
            include_tables=False,
            favor_precision=True,
        )
        return _clean(text)
    except Exception:
        return ""


# ------------------------------------------------------------------------ news

def _resolve_google_news_url(google_url: str) -> str:
    """
    Resolve a news.google.com/rss/articles/<id> URL to the real publisher URL.

    Google stopped embedding the target in the id; it now comes from an
    internal RPC that needs a signature + timestamp scraped from the article
    page. Returns the original URL unchanged if resolution fails.
    """
    if "news.google.com" not in urlparse(google_url).netloc:
        return google_url
    if "/articles/" not in google_url:
        return google_url
    try:
        art_id = google_url.split("/articles/")[1].split("?")[0]
        page = requests.get(
            f"https://news.google.com/rss/articles/{art_id}",
            headers=BROWSER_HEADERS, timeout=ARTICLE_TIMEOUT,
        )
        sig = re.search(r'data-n-a-sg="([^"]+)"', page.text)
        ts = re.search(r'data-n-a-ts="([^"]+)"', page.text)
        if not (sig and ts):
            return google_url
        inner = json.dumps([
            "garturlreq",
            [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1,
              None, None, None, None, None, 0, 1],
             "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0],
            art_id, int(ts.group(1)), sig.group(1),
        ])
        f_req = json.dumps([[["Fbv4je", inner, None, "generic"]]])
        rpc = requests.post(
            "https://news.google.com/_/DotsSplashUi/data/batchexecute",
            headers={
                **BROWSER_HEADERS,
                "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            },
            data={"f.req": f_req}, timeout=ARTICLE_TIMEOUT,
        )
        m = re.search(r'\[\\"garturlres\\",\\"(https?:.*?)\\"', rpc.text)
        if m:
            return m.group(1).encode().decode("unicode_escape")
    except Exception:
        pass
    return google_url


def fetch_google_news(company_name: str, max_items: int = 18) -> list[dict[str, Any]]:
    """Recent Google News RSS results tuned to infrastructure / supply chain."""
    query = (
        f'{company_name} (capex OR "capital expenditure" OR "supply chain" OR '
        f'expansion OR factory OR plant OR "data center" OR reactor OR mine OR '
        f'fab OR offtake OR bottleneck OR shortage)'
    )
    url = (
        "https://news.google.com/rss/search"
        f"?q={quote_plus(query)}&hl=en-US&gl=US&ceid=US:en"
    )
    try:
        resp = requests.get(url, headers=BROWSER_HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"[scraper] {company_name}: Google News failed ({exc}).")
        return []

    feed = feedparser.parse(resp.content)
    items: list[dict[str, Any]] = []
    for entry in feed.entries[:max_items]:
        src = ""
        if entry.get("source") and isinstance(entry.source, dict):
            src = entry.source.get("title", "")
        items.append(
            {
                "title": entry.get("title", ""),
                "summary": "",              # filled by _enrich
                "url": entry.get("link", ""),
                "source": f"News{f' / {src}' if src else ''}",
            }
        )
    return items


# ------------------------------------------------------------------------- SEC

def _cik_map() -> dict[str, str]:
    global _CIK_MAP
    if _CIK_MAP is None:
        _CIK_MAP = {}
        try:
            resp = requests.get(
                "https://www.sec.gov/files/company_tickers.json",
                headers=SEC_HEADERS, timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            for row in resp.json().values():
                _CIK_MAP[row["ticker"].upper()] = f"{int(row['cik_str']):010d}"
        except (requests.RequestException, ValueError, KeyError) as exc:
            print(f"[scraper] SEC ticker map unavailable ({exc}).")
    return _CIK_MAP


def fetch_sec_filings(ticker: str, max_filings: int = 3) -> list[dict[str, Any]]:
    """
    Recent 8-K / 10-K / 10-Q filings for a US ticker, with primary-document text.
    """
    if not _is_us_ticker(ticker):
        print(f"[scraper] {ticker}: non-US ticker, skipping EDGAR.")
        return []

    cik = _cik_map().get(ticker.upper())
    if not cik:
        print(f"[scraper] {ticker}: no CIK on file, skipping EDGAR.")
        return []

    try:
        resp = requests.get(
            f"https://data.sec.gov/submissions/CIK{cik}.json",
            headers=SEC_HEADERS, timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        recent = resp.json().get("filings", {}).get("recent", {})
    except (requests.RequestException, ValueError) as exc:
        print(f"[scraper] {ticker}: EDGAR submissions failed ({exc}).")
        return []

    forms = recent.get("form", [])
    accnos = recent.get("accessionNumber", [])
    docs = recent.get("primaryDocument", [])
    dates = recent.get("filingDate", [])

    items: list[dict[str, Any]] = []
    for form, accno, doc, date in zip(forms, accnos, docs, dates):
        if form not in TARGET_FORMS or not doc:
            continue
        doc_url = (
            f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
            f"{accno.replace('-', '')}/{doc}"
        )
        text = extract_article_text(doc_url, headers=SEC_HEADERS)
        time.sleep(0.3)  # SEC fair-access
        if len(text) < MIN_BODY_CHARS:
            continue
        items.append(
            {
                "title": f"SEC {form} filed {date} — {ticker.upper()}",
                "summary": text,
                "url": doc_url,
                "source": "SEC EDGAR",
            }
        )
        if len(items) >= max_filings:
            break
    return items


# --------------------------------------------------------------------- gather

def _enrich(items: list[dict[str, Any]], limit: int) -> None:
    """Resolve redirect URLs and fill empty summaries with article text."""
    enriched = 0
    for item in items[:limit]:
        if item.get("summary"):
            continue
        url = item.get("url", "")
        if not url:
            continue
        resolved = _resolve_google_news_url(url)
        if resolved != url:
            item["url"] = resolved
            url = resolved
        if "news.google.com" in urlparse(url).netloc:
            continue  # still unresolved -> no article body available
        text = extract_article_text(url)
        if text:
            item["summary"] = text
            enriched += 1
        time.sleep(0.2)
    if enriched:
        print(f"[scraper]   extracted full text for {enriched} article(s).")


def gather_raw_items(
    ticker: str, company_name: str, skip_seen: bool = True, enrich_limit: int = 12
) -> ScrapeResult:
    """
    Aggregate news + SEC items for one company, with body text extracted.

    Returns ScrapeResult(items, collected); ``collected`` is the unique item
    count before the seen-URL filter (recorded for quota-savings auditing).
    """
    # News first: it carries the timely CapEx / supply-chain signal.
    items: list[dict[str, Any]] = []
    items.extend(fetch_google_news(company_name))
    items.extend(fetch_sec_filings(ticker))

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

    # Fetch bodies only for the items we'll actually grade.
    _enrich(deduped, enrich_limit)

    # Drop items with no usable body (dead links, paywalls, unresolved redirects).
    usable = [
        it for it in deduped
        if len((it.get("summary") or "").strip()) >= 60
    ]
    dropped = len(deduped) - len(usable)

    print(
        f"[scraper] {company_name} ({ticker}): {collected} collected, "
        f"{len(usable)} new with text"
        + (f" ({dropped} dropped: no body)" if dropped else "")
        + "."
    )
    return ScrapeResult(items=usable, collected=collected)
