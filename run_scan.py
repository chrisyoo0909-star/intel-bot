"""
Market intelligence scan — main execution entry point.

Workflow:
  1. Seed the company queue if empty.
  2. Pull the 10 companies with the oldest last_scanned timestamps.
  3. Scrape recent SEC filings + Google News for each.
  4. Grade every raw item through the Gemini analyzer.
  5. Persist items scoring >= 8 into the 'signals' table.
  6. Stamp last_scanned for every processed company.
  7. Print progress and a final summary.

Run:  python run_scan.py
"""

from __future__ import annotations

import time
import traceback

from analyzer import evaluate_item
from db import (
    get_next_company_batch,
    log_scan,
    mark_items_seen,
    prune_scan_logs,
    prune_seen_urls,
    save_signal,
    seed_company_queue,
    update_scan_timestamp,
)
from scraper import gather_raw_items

BATCH_SIZE = 10
MAX_ITEMS_PER_COMPANY = 15


def _rule(char: str = "-") -> None:
    print(char * 64)


def scan() -> None:
    print("MARKET INTELLIGENCE SCAN")
    _rule("=")

    seeded = seed_company_queue()
    if seeded:
        print(f"Queue seeded with {seeded} companies.")

    batch = get_next_company_batch(limit=BATCH_SIZE)
    if not batch:
        print("No companies in queue. Nothing to do.")
        return

    print(f"Scanning {len(batch)} companies:")
    for c in batch:
        print(f"  - {c['company_name']} ({c['ticker']}) / {c['domain']}")
    _rule()

    processed_ids: list[int] = []
    total_items = 0
    total_signals = 0
    saved_signals: list[dict] = []

    for idx, company in enumerate(batch, start=1):
        cid = company["id"]
        name = company["company_name"]
        ticker = company["ticker"]
        domain = company["domain"]

        print(f"\n[{idx}/{len(batch)}] {name} ({ticker}) — {domain}")

        try:
            raw_items = gather_raw_items(ticker, name)
        except Exception as exc:
            print(f"  ! scrape failed: {exc}")
            traceback.print_exc()
            log_scan(ticker, name, domain, raw_items_count=0, signals_found=0)
            processed_ids.append(cid)
            continue

        raw_items = raw_items[:MAX_ITEMS_PER_COMPANY]
        company_signals = 0
        graded_items: list[dict] = []

        for j, item in enumerate(raw_items, start=1):
            total_items += 1
            snippet = f"{item.get('title', '')}\n{item.get('summary', '')}".strip()
            url = item.get("url", "")

            verdict = evaluate_item(name, domain, snippet, url)
            # Only fingerprint items Gemini actually judged; a transient API
            # error leaves the URL unseen so the next run retries it.
            if "error" not in verdict:
                graded_items.append(item)
            score = verdict["conviction_score"]
            flag = "SIGNAL" if verdict["valid"] else "filler"
            print(
                f"    ({j}/{len(raw_items)}) [{score}/10 {flag}] "
                f"{item.get('title', '')[:70]}"
            )

            if verdict["valid"]:
                written = save_signal(
                    company=name,
                    domain=domain,
                    score=score,
                    headline=verdict["headline"],
                    analysis=verdict["analysis"],
                    url=url,
                )
                if written:
                    company_signals += 1
                    total_signals += 1
                    saved_signals.append(
                        {
                            "company": name,
                            "score": score,
                            "headline": verdict["headline"],
                        }
                    )

            # Gentle pacing for the free Gemini tier.
            time.sleep(1.0)

        # Fingerprint every graded URL so it is never re-sent to Gemini.
        mark_items_seen(graded_items, company_symbol=ticker)

        # Audit-log this company's outcome immediately after it finishes.
        log_scan(
            company_symbol=ticker,
            company_name=name,
            domain=domain,
            raw_items_count=len(raw_items),
            signals_found=company_signals,
        )

        print(f"  => {company_signals} high-conviction signal(s) from {name}.")
        processed_ids.append(cid)

    update_scan_timestamp(processed_ids)
    prune_scan_logs()
    prune_seen_urls()

    _rule("=")
    print("SCAN COMPLETE")
    print(f"  Companies processed : {len(processed_ids)}")
    print(f"  Raw items graded    : {total_items}")
    print(f"  Signals saved (>=8) : {total_signals}")
    if saved_signals:
        print("\n  Top signals:")
        for s in sorted(saved_signals, key=lambda x: -x["score"]):
            print(f"    [{s['score']}/10] {s['company']}: {s['headline']}")
    else:
        print("\n  No high-conviction breakthroughs this cycle.")
    _rule("=")


if __name__ == "__main__":
    scan()
