"""
Zero-cost data acquisition with full article / filing text.

Sources (no API keys):
  * SEC EDGAR   -> material 8-K items (1.01/1.03/2.01/7.01/8.01) + 6-K / 20-F,
                   primary-document text via data.sec.gov submissions JSON
  * Google News -> recent headlines; redirect URLs are resolved to the real
                   publisher link BEFORE de-duplication, then the body is
                   extracted

Bodies are extracted with trafilatura. Google-redirect resolution happens
before filter_unseen_items() so the dedup hash is always the canonical
publisher URL.
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

    items: list[dict[str, Any]]   # items to grade (post seen-URL filter, with text)
    collected: int                # unique items found before the seen-URL filter
    drops: dict[str, int]         # drop reasons: unresolved_google/paywall_teaser/short_body


BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
BROWSER_HEADERS = {"User-Agent": BROWSER_UA}
SEC_HEADERS = {
    "User-Agent": "ResearchBot admin@investor.com",
    "Accept-Encoding": "gzip, deflate",
}

REQUEST_TIMEOUT = 20
ARTICLE_TIMEOUT = 15
ARTICLE_TEXT_CHARS = 3500
MAX_DOWNLOAD_BYTES = 1_500_000    # abort heavy pages to cap memory
MIN_BODY = 400                    # chars; below this the body is unusable

# Hard paywall / bot-wall markers -> always reject.
_PAYWALL_HARD = (
    "subscribe to read", "subscribe to continue", "metered",
    "cf-browser-verification", "cf_browser_verification",
    "please enable javascript", "create a free account to",
    "this content is for subscribers",
)
# Soft marker -> reject only when the body is also short (real articles use it too).
_PAYWALL_SOFT = ("continue reading",)

# 8-K items worth reading (material agreements / bankruptcy / asset sales /
# Reg FD disclosure / other material events).
_MATERIAL_8K_ITEMS = {"1.01", "1.03", "2.01", "7.01", "8.01"}
_TARGET_FORMS = {"8-K", "8-K/A", "6-K", "6-K/A", "20-F", "20-F/A"}
_SEC_BOILERPLATE_HEAD = re.compile(
    r"^\s*(united states\s+securities and exchange commission|"
    r"table of contents|form\s+(10-k|10-q))", re.I,
)

_CIK_MAP: dict[str, str] | None = None


# --------------------------------------------------------------------- helpers

def _clean(text: str | None, limit: int = ARTICLE_TEXT_CHARS) -> str:
    if not text:
        return ""
    return " ".join(text.split())[:limit]


def _is_us_ticker(ticker: str) -> bool:
    return "." not in ticker and ticker.isalpha()


def _is_google_news(url: str) -> bool:
    return "news.google.com" in urlparse(url or "").netloc


def _download(url: str, headers: dict[str, str] | None = None) -> tuple[str, str]:
    """
    Stream a URL. Returns (html, reason). reason is "" on success or one of
    "too_large" / "http_error" / "network".
    """
    try:
        with requests.get(
            url, headers=headers or BROWSER_HEADERS, timeout=ARTICLE_TIMEOUT,
            stream=True,
        ) as resp:
            resp.raise_for_status()
            total = 0
            chunks: list[bytes] = []
            for chunk in resp.iter_content(65536):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    return "", "too_large"
                chunks.append(chunk)
            enc = resp.encoding or "utf-8"
            return b"".join(chunks).decode(enc, errors="replace"), ""
    except requests.HTTPError:
        return "", "http_error"
    except requests.RequestException:
        return "", "network"


def _looks_paywalled(text: str) -> bool:
    low = text.lower()
    if any(m in low for m in _PAYWALL_HARD):
        return True
    if len(text) < 900 and any(m in low for m in _PAYWALL_SOFT):
        return True
    return False


def extract_article_text(
    url: str, headers: dict[str, str] | None = None
) -> tuple[str, str]:
    """
    Fetch a URL and return (body_text, reason).

    reason is "" on success, else "short_body" / "paywall_teaser" /
    "too_large" / "http_error" / "network" / "sec_boilerplate".
    """
    html, dl_reason = _download(url, headers)
    if dl_reason:
        return "", dl_reason
    text = trafilatura.extract(
        html, include_comments=False, include_tables=False, favor_precision=True
    )
    body = _clean(text)
    if len(body) < MIN_BODY:
        return "", "short_body"
    if _looks_paywalled(body):
        return "", "paywall_teaser"
    if _SEC_BOILERPLATE_HEAD.match(body) and len(body) < 1200:
        return "", "sec_boilerplate"
    return body, ""


# ------------------------------------------------------------------------ news

def _resolve_google_news_url(google_url: str) -> str:
    """
    Resolve a news.google.com/rss/articles/<id> URL to the publisher URL via
    Google's internal RPC. Returns the original URL unchanged on failure.
    """
    if not _is_google_news(google_url) or "/articles/" not in google_url:
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
                "summary": "",
                "url": entry.get("link", ""),
                "source": f"News{f' / {src}' if src else ''}",
                "origin": "news",
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


def _material_8k(form: str, items_field: str) -> bool:
    if form.startswith("6-K") or form.startswith("20-F"):
        return True
    if not form.startswith("8-K"):
        return False
    present = {p.strip() for p in (items_field or "").replace(";", ",").split(",")}
    return bool(present & _MATERIAL_8K_ITEMS)


def fetch_sec_filings(ticker: str, max_filings: int = 3) -> list[dict[str, Any]]:
    """Recent material 8-K / 6-K / 20-F filings with primary-document text."""
    if not _is_us_ticker(ticker):
        # 6-K / 20-F issuers can still be non-alpha; try the map anyway.
        pass
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
    items_col = recent.get("items", [""] * len(forms))

    out: list[dict[str, Any]] = []
    for form, accno, doc, date, itms in zip(forms, accnos, docs, dates, items_col):
        if form not in _TARGET_FORMS or not doc:
            continue
        if not _material_8k(form, itms):
            continue
        doc_url = (
            f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
            f"{accno.replace('-', '')}/{doc}"
        )
        text, reason = extract_article_text(doc_url, headers=SEC_HEADERS)
        time.sleep(0.3)  # SEC fair-access
        if reason:
            continue
        label = f"SEC {form} ({itms})" if itms else f"SEC {form}"
        out.append(
            {
                "title": f"{label} filed {date} — {ticker.upper()}",
                "summary": text,
                "url": doc_url,
                "source": "SEC EDGAR",
                "origin": "sec",
            }
        )
        if len(out) >= max_filings:
            break
    return out


# --------------------------------------------------------------------- gather

def gather_raw_items(
    ticker: str,
    company_name: str,
    skip_seen: bool = True,
    budget: int = 6,
) -> ScrapeResult:
    """
    Aggregate SEC + news for one company, bodies extracted.

    Order: up to 3 material SEC filings first, then resolved news fills the
    remaining budget. Google redirects are resolved BEFORE the seen-URL filter
    so dedup hashes the canonical publisher URL.
    """
    drops = {"unresolved_google": 0, "paywall_teaser": 0, "short_body": 0}

    sec_items = fetch_sec_filings(ticker, max_filings=3)  # already have text
    news_items = fetch_google_news(company_name)

    # Resolve Google-redirect URLs up front (before dedup / seen filter).
    for it in news_items:
        if _is_google_news(it["url"]):
            resolved = _resolve_google_news_url(it["url"])
            if _is_google_news(resolved):
                it["_unresolved"] = True
            else:
                it["url"] = resolved
            time.sleep(0.15)

    ordered = sec_items + news_items

    # In-batch dedup on the (now canonical) URL.
    seen_local: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for it in ordered:
        key = it.get("url") or it.get("title", "")
        if key in seen_local:
            continue
        seen_local.add(key)
        deduped.append(it)

    collected = len(deduped)

    if skip_seen:
        try:
            from db import filter_unseen_items

            deduped = filter_unseen_items(deduped)
        except Exception as exc:
            print(f"[scraper] seen-URL filter unavailable ({exc}); grading all.")

    # Build the graded set: SEC (already has text) + news bodies up to budget.
    usable: list[dict[str, Any]] = []
    extracted = 0
    for it in deduped:
        if len(usable) >= budget:
            break
        if it.get("origin") == "sec" and it.get("summary"):
            usable.append(it)
            continue
        if it.get("_unresolved") or _is_google_news(it.get("url", "")):
            drops["unresolved_google"] += 1
            continue
        body, reason = extract_article_text(it["url"])
        time.sleep(0.2)
        if reason == "paywall_teaser":
            drops["paywall_teaser"] += 1
            continue
        if reason in ("short_body", "sec_boilerplate", "too_large", "http_error", "network"):
            drops["short_body"] += 1
            continue
        it["summary"] = body
        extracted += 1
        usable.append(it)

    if extracted:
        print(f"[scraper]   extracted full text for {extracted} article(s).")
    drop_str = ", ".join(f"{k}={v}" for k, v in drops.items() if v)
    print(
        f"[scraper] {company_name} ({ticker}): {collected} collected, "
        f"{len(usable)} to grade"
        + (f" (dropped: {drop_str})" if drop_str else "")
        + "."
    )
    return ScrapeResult(items=usable, collected=collected, drops=drops)
