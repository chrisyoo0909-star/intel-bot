"""
Supabase persistence layer for the market intelligence pipeline.

Expected Supabase schema (canonical copy lives in schema.sql — run that in the
Supabase SQL editor; the outline below is kept in sync for reference):

    create table if not exists company_queue (
        id            bigint generated always as identity primary key,
        ticker        text not null unique,
        company_name  text not null,
        domain        text not null,
        last_scanned  timestamptz not null default '1970-01-01T00:00:00Z'
    );

    create table if not exists signals (
        id                       bigint generated always as identity primary key,
        company                  text not null,
        domain                   text not null,
        conviction_score         int  not null,
        headline                 text not null,
        analysis                 text not null,
        url                      text,
        recommendation           text,
        price_target_delta_pct   numeric,
        implied_price_target     numeric,
        supply_chain_driver      text,
        financial_impact_thesis  text,
        created_at               timestamptz not null default now()
    );
    -- One signal per story: two unique constraints, upserted against.
    alter table signals add constraint signals_company_headline_key unique (company, headline);
    alter table signals add constraint signals_url_key unique (url);

    create table if not exists scan_logs (
        id                 uuid primary key default gen_random_uuid(),
        company_symbol     text,
        company_name       text,
        domain             text,
        raw_collected      int  not null default 0,  -- items found before dedup
        raw_items_count    int  not null default 0,  -- items actually graded
        signals_found      int  not null default 0,
        dropped_unresolved int  not null default 0,
        dropped_paywall    int  not null default 0,
        dropped_short      int  not null default 0,
        scanned_at         timestamptz not null default now()
    );
    create index if not exists scan_logs_scanned_at_idx
        on scan_logs (scanned_at desc);

    -- Fingerprints of every article/filing already graded, so the scraper
    -- never sends the same URL through the LLM twice.
    create table if not exists seen_urls (
        url_hash        text primary key,
        url             text,
        company_symbol  text,
        first_seen_at   timestamptz not null default now()
    );
    create index if not exists seen_urls_first_seen_at_idx
        on seen_urls (first_seen_at desc);

    -- Single-row advisory lock so two scans never run concurrently.
    create table if not exists scan_state (
        id            text primary key,
        locked_at     timestamptz,
        locked_until  timestamptz not null default '1970-01-01T00:00:00Z',
        host          text
    );

Credentials are read strictly from the local .env file.
"""

from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from urllib.parse import urlparse, urlsplit, urlunsplit

from dotenv import load_dotenv
from supabase import Client, create_client

# Load .env from the project directory regardless of the current working dir.
load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
load_dotenv()  # fall back to default lookup, does not override already-set vars


def _clean_env(name: str) -> str:
    """Read an env var and strip quotes, whitespace, and trailing slashes."""
    raw = os.environ.get(name, "")
    value = raw.strip().strip("'").strip('"').strip()
    return value.rstrip("/")


def _sanitize_supabase_url(url: str) -> str:
    """
    Normalize the Supabase project URL to a bare 'https://<ref>.supabase.co'.

    Fixes the common misconfigurations that cause 'getaddrinfo failed':
      - missing/!= https scheme
      - duplicated 'https://https://' prefixes
      - a trailing '/rest/v1/' (or any) path copied from the API docs
    """
    value = url.strip().strip("'").strip('"').strip().rstrip("/")

    # Collapse duplicated scheme prefixes, e.g. "https://https://abc.supabase.co".
    while value.lower().startswith("https://https://"):
        value = value[len("https://"):]
    while value.lower().startswith("http://http://"):
        value = value[len("http://"):]

    if value.startswith("http://"):
        value = "https://" + value[len("http://"):]
    if not value.startswith("https://"):
        value = "https://" + value.lstrip("/")

    parsed = urlparse(value)
    host = parsed.netloc or parsed.path.split("/")[0]
    if not host:
        raise RuntimeError(f"Could not parse a host from SUPABASE_URL={url!r}")

    # Drop any path such as '/rest/v1/' — the client appends its own paths.
    normalized = f"https://{host}"

    if not normalized.startswith("https://"):
        raise RuntimeError(f"SUPABASE_URL must start with https:// (got {url!r})")
    if normalized.count("https://") != 1 or "/rest/v1" in normalized:
        raise RuntimeError(f"SUPABASE_URL still malformed after cleanup: {normalized!r}")

    return normalized


SUPABASE_URL = _sanitize_supabase_url(_clean_env("SUPABASE_URL"))
SUPABASE_KEY = _clean_env("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError(
        "SUPABASE_URL and SUPABASE_KEY must be set in the local .env file."
    )

print(f"Connecting to Supabase host: {urlparse(SUPABASE_URL).netloc} ...")

EPOCH = "1970-01-01T00:00:00+00:00"

# 30 tickers across the 5 target intelligence domains.
SEED_COMPANIES: list[dict[str, str]] = [
    # Advanced Compute
    {"ticker": "TSM", "company_name": "Taiwan Semiconductor Manufacturing", "domain": "Advanced Compute"},
    {"ticker": "ASML", "company_name": "ASML Holding", "domain": "Advanced Compute"},
    {"ticker": "NVDA", "company_name": "NVIDIA", "domain": "Advanced Compute"},
    {"ticker": "MU", "company_name": "Micron Technology", "domain": "Advanced Compute"},
    {"ticker": "AMAT", "company_name": "Applied Materials", "domain": "Advanced Compute"},
    # Hyperscale Cloud
    {"ticker": "GOOGL", "company_name": "Alphabet", "domain": "Hyperscale Cloud"},
    {"ticker": "AMZN", "company_name": "Amazon", "domain": "Hyperscale Cloud"},
    {"ticker": "MSFT", "company_name": "Microsoft", "domain": "Hyperscale Cloud"},
    {"ticker": "META", "company_name": "Meta Platforms", "domain": "Hyperscale Cloud"},
    {"ticker": "ORCL", "company_name": "Oracle", "domain": "Hyperscale Cloud"},
    # Critical Energy
    {"ticker": "CEG", "company_name": "Constellation Energy", "domain": "Critical Energy"},
    {"ticker": "NEE", "company_name": "NextEra Energy", "domain": "Critical Energy"},
    {"ticker": "VST", "company_name": "Vistra", "domain": "Critical Energy"},
    {"ticker": "CCJ", "company_name": "Cameco", "domain": "Critical Energy"},
    {"ticker": "GE", "company_name": "GE Aerospace", "domain": "Critical Energy"},
    # Critical Minerals
    {"ticker": "BHP", "company_name": "BHP Group", "domain": "Critical Minerals"},
    {"ticker": "RIO", "company_name": "Rio Tinto", "domain": "Critical Minerals"},
    {"ticker": "FCX", "company_name": "Freeport-McMoRan", "domain": "Critical Minerals"},
    {"ticker": "MP", "company_name": "MP Materials", "domain": "Critical Minerals"},
    {"ticker": "ALB", "company_name": "Albemarle", "domain": "Critical Minerals"},
    # Physical AI & Robotics
    {"ticker": "TSLA", "company_name": "Tesla", "domain": "Physical AI & Robotics"},
    {"ticker": "002594.SZ", "company_name": "BYD", "domain": "Physical AI & Robotics"},
    {"ticker": "ISRG", "company_name": "Intuitive Surgical", "domain": "Physical AI & Robotics"},
    {"ticker": "SYM", "company_name": "Symbotic", "domain": "Physical AI & Robotics"},
    {"ticker": "6954.T", "company_name": "Fanuc", "domain": "Physical AI & Robotics"},
    # Depth picks to reach 30 across the same domains
    {"ticker": "LRCX", "company_name": "Lam Research", "domain": "Advanced Compute"},
    {"ticker": "AVGO", "company_name": "Broadcom", "domain": "Advanced Compute"},
    {"ticker": "SMCI", "company_name": "Super Micro Computer", "domain": "Hyperscale Cloud"},
    {"ticker": "TLN", "company_name": "Talen Energy", "domain": "Critical Energy"},
    {"ticker": "SCCO", "company_name": "Southern Copper", "domain": "Critical Minerals"},
]


def get_client() -> Client:
    """Return a configured Supabase client."""
    return create_client(SUPABASE_URL, SUPABASE_KEY)


_supabase: Client = get_client()


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _chunks(seq: list[Any], size: int) -> Iterable[list[Any]]:
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def normalize_url(url: str) -> str:
    """Canonicalize a URL for stable de-duplication."""
    raw = (url or "").strip()
    if not raw:
        return ""
    parts = urlsplit(raw)
    netloc = parts.netloc.lower()
    path = parts.path.rstrip("/") or "/"
    # Canonicalize scheme to https and drop query/fragment: news aggregators
    # serve the same article under http/https and with volatile tracking params.
    return urlunsplit(("https", netloc, path, "", ""))


def url_hash(url: str) -> str:
    """SHA-256 fingerprint of the normalized URL."""
    return hashlib.sha256(normalize_url(url).encode("utf-8")).hexdigest()


def seed_company_queue() -> int:
    """
    Seed 'company_queue' with the target universe if the table is empty.

    Returns the number of rows inserted (0 when the queue was already populated).
    """
    existing = (
        _supabase.table("company_queue").select("id", count="exact").limit(1).execute()
    )
    count = existing.count or 0
    if count > 0:
        print(f"[db] company_queue already seeded ({count} rows). Skipping.")
        return 0

    rows = [
        {
            "ticker": c["ticker"],
            "company_name": c["company_name"],
            "domain": c["domain"],
            "last_scanned": EPOCH,
        }
        for c in SEED_COMPANIES
    ]
    _supabase.table("company_queue").insert(rows).execute()
    print(f"[db] Seeded company_queue with {len(rows)} companies.")
    return len(rows)


def get_next_company_batch(limit: int = 10) -> list[dict[str, Any]]:
    """
    Return the least-recently-scanned companies (FIFO rotation).

    Ordered by ``last_scanned ASC NULLS FIRST`` so never-scanned companies are
    always picked up before any that already carry a timestamp.
    """
    base = _supabase.table("company_queue").select(
        "id, ticker, company_name, domain, last_scanned"
    )
    try:
        resp = (
            base.order("last_scanned", desc=False, nullsfirst=True)
            .limit(limit)
            .execute()
        )
    except TypeError:
        # Older postgrest-py lacks the nullsfirst kwarg. Seeded rows use an
        # explicit epoch timestamp rather than NULL, so plain ASC still puts
        # never-scanned companies first.
        resp = base.order("last_scanned", desc=False).limit(limit).execute()
    return resp.data or []


def update_scan_timestamp(company_ids: Iterable[int]) -> None:
    """Set 'last_scanned' to the current UTC time for the given company ids."""
    ids = [int(i) for i in company_ids]
    if not ids:
        return
    (
        _supabase.table("company_queue")
        .update({"last_scanned": _utcnow_iso()})
        .in_("id", ids)
        .execute()
    )
    print(f"[db] Updated last_scanned for {len(ids)} companies.")


def _to_number(value: Any) -> float | None:
    """Best-effort numeric coercion; returns None for anything unusable."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        num = float(value)
    else:
        text = str(value).strip().replace("%", "").replace("$", "").replace(",", "")
        text = text.lstrip("+")
        m = re.search(r"-?\d+(?:\.\d+)?", text)
        if not m:
            return None
        try:
            num = float(m.group(0))
        except ValueError:
            return None
    if num != num or num in (float("inf"), float("-inf")):
        return None
    return round(num, 4)


def save_signal(
    company: str,
    domain: str,
    score: int,
    headline: str,
    analysis: str,
    url: str | None,
    recommendation: str | None = None,
    supply_chain_driver: str | None = None,
    price_target_delta_pct: Any = None,
    implied_price_target: Any = None,
    financial_impact_thesis: str | None = None,
) -> bool:
    """
    Persist a high-conviction signal (score >= 8) into the 'signals' table.

    The table carries two unique constraints — UNIQUE(url) and
    UNIQUE(company, headline). This upserts on 'url' when a URL is present (one
    signal per source story) and falls back to updating the (company, headline)
    row if that constraint is the one that collides. 'created_at' is omitted so
    the original first-seen timestamp is preserved on updates.

    Numeric fields are coerced with a graceful fallback to NULL — a bad model
    value never breaks the write.

    Returns True on a successful write/update. Raises on an unhandled DB error
    (the caller must then NOT mark the item seen).
    """
    if score < 8:
        return False
    row = {
        "company": company,
        "domain": domain,
        "conviction_score": int(score),
        "headline": headline,
        "analysis": analysis,
        "url": url or None,
        "recommendation": (recommendation or None),
        "supply_chain_driver": (supply_chain_driver or None),
        "price_target_delta_pct": _to_number(price_target_delta_pct),
        "implied_price_target": _to_number(implied_price_target),
        "financial_impact_thesis": (financial_impact_thesis or None),
    }

    def _core(r: dict[str, Any]) -> dict[str, Any]:
        return {k: r[k] for k in (
            "company", "domain", "conviction_score", "headline", "analysis", "url"
        )}

    def _write(r: dict[str, Any]) -> None:
        conflict = "url" if r.get("url") else "company,headline"
        try:
            _supabase.table("signals").upsert(r, on_conflict=conflict).execute()
        except Exception as exc:
            msg = str(exc).lower()
            if "column" in msg or "schema cache" in msg:
                # analyst columns not migrated yet -> core fields only
                _supabase.table("signals").upsert(
                    _core(r), on_conflict=conflict
                ).execute()
                print("[db] (core fields only — run schema.sql)")
                return
            if ("company_headline" in msg or "company, headline" in msg
                    or "signals_company_headline_key" in msg):
                # the OTHER constraint collided: update that row in place
                upd = {k: v for k, v in r.items() if k != "url"}
                _supabase.table("signals").update(upd).eq(
                    "company", r["company"]
                ).eq("headline", r["headline"]).execute()
                return
            raise

    _write(row)
    tgt = row["implied_price_target"]
    dlt = row["price_target_delta_pct"]
    extra = f" | {row['recommendation']}"
    if dlt is not None:
        extra += f" {dlt:+.1f}%"
    if tgt is not None:
        extra += f" -> {tgt}"
    print(f"[db] Saved signal [{score}/10] {company}: {headline}{extra}")
    return True


def fetch_signals(
    domain: str | None = None, min_score: int = 8
) -> list[dict[str, Any]]:
    """Read high-conviction signals for the dashboard, newest first."""
    query = (
        _supabase.table("signals")
        .select("*")
        .gte("conviction_score", min_score)
        .order("created_at", desc=True)
    )
    if domain and domain != "All":
        query = query.eq("domain", domain)
    resp = query.execute()
    return resp.data or []


def fetch_top_picks(limit: int = 6, days: int = 30) -> list[dict[str, Any]]:
    """
    Executive top picks: the highest-conviction recent signals, ranked by
    conviction then expected 12-month upside.

    Reads the 'top_picks' view when present (see schema.sql) and otherwise
    falls back to ranking the 'signals' table directly.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    try:
        resp = (
            _supabase.table("top_picks")
            .select("*")
            .gte("created_at", since)
            .limit(limit)
            .execute()
        )
        if resp.data:
            return resp.data
    except Exception:
        pass

    base = (
        _supabase.table("signals")
        .select("*")
        .gte("conviction_score", 8)
        .gte("created_at", since)
        .order("conviction_score", desc=True)
    )
    try:
        resp = (
            base.order("price_target_delta_pct", desc=True, nullsfirst=False)
            .order("created_at", desc=True).limit(limit).execute()
        )
    except TypeError:
        resp = (
            base.order("price_target_delta_pct", desc=True)
            .order("created_at", desc=True).limit(limit).execute()
        )
    return resp.data or []


# ------------------------------------------------------------------ scan_logs

SCAN_LOG_RETENTION = 500  # keep the newest N audit rows (FIFO rotation)


def log_scan(
    company_symbol: str,
    company_name: str,
    domain: str,
    raw_items_count: int,
    signals_found: int,
    raw_collected: int | None = None,
    drops: dict[str, int] | None = None,
) -> None:
    """
    Record one company's scan outcome in the 'scan_logs' audit table.

    ``raw_collected`` is the item count found before URL de-duplication;
    ``raw_items_count`` is what was actually sent to the LLM. ``drops`` counts
    items discarded by the scraper, keyed unresolved_google / paywall_teaser /
    short_body.
    """
    drops = drops or {}
    row = {
        "company_symbol": company_symbol,
        "company_name": company_name,
        "domain": domain,
        "raw_collected": int(raw_collected if raw_collected is not None else raw_items_count),
        "raw_items_count": int(raw_items_count),
        "signals_found": int(signals_found),
        "dropped_unresolved": int(drops.get("unresolved_google", 0)),
        "dropped_paywall": int(drops.get("paywall_teaser", 0)),
        "dropped_short": int(drops.get("short_body", 0)),
        "scanned_at": _utcnow_iso(),
    }
    try:
        _supabase.table("scan_logs").insert(row).execute()
    except Exception as exc:
        msg = str(exc).lower()
        if "column" in msg or "schema cache" in msg:
            legacy = {k: row[k] for k in (
                "company_symbol", "company_name", "domain", "raw_collected",
                "raw_items_count", "signals_found", "scanned_at",
            )}
            try:
                _supabase.table("scan_logs").insert(legacy).execute()
            except Exception as exc2:
                print(f"[db] WARNING: scan_logs write failed for {company_symbol}: {exc2}")
                return
        else:
            print(f"[db] WARNING: could not write scan_logs for {company_symbol}: {exc}")
            return
    drop_str = ", ".join(
        f"{k}={row[k]}" for k in ("dropped_unresolved", "dropped_paywall", "dropped_short")
        if row[k]
    )
    print(
        f"[db] scan_logs += {company_symbol} "
        f"(collected={row['raw_collected']}, graded={raw_items_count}, "
        f"signals={signals_found}" + (f", {drop_str}" if drop_str else "") + ")"
    )


def prune_scan_logs(keep: int = SCAN_LOG_RETENTION) -> int:
    """
    FIFO-rotate 'scan_logs': delete every row older than the newest ``keep``.

    Returns the number of rows deleted (0 when under the cap or on error).
    """
    try:
        # Grab the single row sitting just past the retention window.
        anchor = (
            _supabase.table("scan_logs")
            .select("scanned_at")
            .order("scanned_at", desc=True)
            .range(keep, keep)
            .execute()
        )
        if not anchor.data:
            return 0
        cutoff = anchor.data[0]["scanned_at"]
        deleted = (
            _supabase.table("scan_logs")
            .delete()
            .lt("scanned_at", cutoff)
            .execute()
        )
        n = len(deleted.data or [])
        if n:
            print(f"[db] Pruned {n} old scan_logs rows (retention={keep}).")
        return n
    except Exception as exc:
        print(f"[db] WARNING: scan_logs prune skipped: {exc}")
        return 0


def fetch_scan_logs(limit: int = 50) -> list[dict[str, Any]]:
    """Read the most recent scan audit rows, newest first."""
    cols = ("scanned_at, company_symbol, company_name, domain, raw_collected, "
            "raw_items_count, signals_found, dropped_unresolved, "
            "dropped_paywall, dropped_short")
    try:
        resp = (
            _supabase.table("scan_logs").select(cols)
            .order("scanned_at", desc=True).limit(limit).execute()
        )
    except Exception:
        resp = (
            _supabase.table("scan_logs")
            .select("scanned_at, company_symbol, company_name, domain, "
                    "raw_collected, raw_items_count, signals_found")
            .order("scanned_at", desc=True).limit(limit).execute()
        )
    return resp.data or []


# ------------------------------------------------------------------ seen_urls

SEEN_URL_RETENTION_DAYS = 120  # forget fingerprints older than this


def filter_unseen_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Return only the raw items whose URL has never been graded before.

    Items without a URL are always kept (they cannot be de-duplicated).
    On any Supabase error the full list is returned unchanged so a transient
    outage never silently drops coverage.
    """
    if not items:
        return items

    hashed = [(it, url_hash(it["url"])) for it in items if it.get("url")]
    unique_hashes = sorted({h for _, h in hashed})
    if not unique_hashes:
        return items

    seen: set[str] = set()
    try:
        for chunk in _chunks(unique_hashes, 100):
            resp = (
                _supabase.table("seen_urls")
                .select("url_hash")
                .in_("url_hash", chunk)
                .execute()
            )
            seen.update(r["url_hash"] for r in (resp.data or []))
    except Exception as exc:
        print(f"[db] WARNING: seen_urls lookup failed, grading all items: {exc}")
        return items

    kept: list[dict[str, Any]] = []
    skipped = 0
    for it in items:
        u = it.get("url")
        if u and url_hash(u) in seen:
            skipped += 1
            continue
        kept.append(it)

    if skipped:
        print(f"[db] Skipped {skipped} already-seen item(s); {len(kept)} new.")
    return kept


def mark_items_seen(
    items: list[dict[str, Any]], company_symbol: str | None = None
) -> None:
    """Record the URLs of items that have now been graded."""
    rows: dict[str, dict[str, Any]] = {}
    for it in items:
        u = it.get("url")
        if not u:
            continue
        h = url_hash(u)
        rows[h] = {
            "url_hash": h,
            "url": normalize_url(u),
            "company_symbol": company_symbol,
        }
    if not rows:
        return
    try:
        for chunk in _chunks(list(rows.values()), 200):
            (
                _supabase.table("seen_urls")
                .upsert(chunk, on_conflict="url_hash", ignore_duplicates=True)
                .execute()
            )
        print(f"[db] Recorded {len(rows)} URL fingerprint(s) in seen_urls.")
    except Exception as exc:
        print(f"[db] WARNING: could not update seen_urls: {exc}")


def prune_seen_urls(days: int = SEEN_URL_RETENTION_DAYS) -> int:
    """Delete URL fingerprints older than ``days`` to keep the table bounded."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    try:
        deleted = (
            _supabase.table("seen_urls")
            .delete()
            .lt("first_seen_at", cutoff)
            .execute()
        )
        n = len(deleted.data or [])
        if n:
            print(f"[db] Pruned {n} seen_urls rows older than {days}d.")
        return n
    except Exception as exc:
        print(f"[db] WARNING: seen_urls prune skipped: {exc}")
        return 0


# ----------------------------------------------------------------- scan_state

_LOCK_ID = "singleton"


def acquire_scan_lock(minutes: int = 45) -> bool:
    """
    Best-effort advisory lock so two scans never run concurrently.

    Returns True if this process now holds the lock. The lock auto-expires after
    ``minutes`` so a crashed run cannot wedge the pipeline forever. Race-safe on
    the update path (Postgres serializes ``update ... where locked_until < now``);
    on any Supabase error it returns True (fail-open — a missed lock is better
    than a skipped scan).
    """
    now = datetime.now(timezone.utc)
    until = (now + timedelta(minutes=minutes)).isoformat()
    host = os.environ.get("GITHUB_RUN_ID") or os.environ.get("COMPUTERNAME") or "local"
    row = {"id": _LOCK_ID, "locked_at": now.isoformat(), "locked_until": until, "host": host}
    try:
        # Take it if the row is missing or the existing lock has expired.
        taken = (
            _supabase.table("scan_state")
            .update(row)
            .eq("id", _LOCK_ID)
            .lt("locked_until", now.isoformat())
            .execute()
        )
        if taken.data:
            return True
        try:
            _supabase.table("scan_state").insert(row).execute()
            return True  # first run ever
        except Exception:
            pass  # row exists and is not expired -> someone holds it
        held = (
            _supabase.table("scan_state")
            .select("locked_until, host")
            .eq("id", _LOCK_ID)
            .execute()
        )
        if held.data:
            print(f"[db] scan lock held by {held.data[0].get('host')} "
                  f"until {held.data[0].get('locked_until')}.")
        return False
    except Exception as exc:
        print(f"[db] WARNING: scan lock check failed, proceeding anyway: {exc}")
        return True


def release_scan_lock() -> None:
    """Release the advisory lock (idempotent)."""
    try:
        _supabase.table("scan_state").update(
            {"locked_until": EPOCH}
        ).eq("id", _LOCK_ID).execute()
    except Exception as exc:
        print(f"[db] WARNING: could not release scan lock: {exc}")
