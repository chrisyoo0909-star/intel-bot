"""
Market intelligence scan — main execution entry point.

Workflow:
  1. Seed the company queue if empty.
  2. Pull the oldest-scanned batch of companies.
  3. Scrape recent SEC filings + Google News for each (skipping URLs already seen).
  4. Grade every new item through the LLM analyzer.
  5. Upsert items scoring >= 8 into the 'signals' table.
  6. Fingerprint graded URLs, audit-log the company, stamp last_scanned.
  7. Print progress + a final summary.

Exit codes:
  0  scan completed and the LLM was healthy
  1  LLM quota exhausted mid-run, or > 50% of LLM calls failed
     (so a scheduled GitHub Actions run turns red instead of failing silently)

Run:  python run_scan.py
"""

from __future__ import annotations

import sys
import time
import traceback

from analyzer import LLMQuotaError, evaluate_item
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
MAX_ITEMS_PER_COMPANY = 8       # cap LLM calls per company (free-tier budget)
LLM_FAILURE_ABORT_RATIO = 0.5   # fail the run if this share of calls error out
ITEM_PACING_SECONDS = 2.0       # keep under ~30 requests/min on free tiers


def _rule(char: str = "-") -> None:
    print(char * 64)


class ScanStats:
    def __init__(self) -> None:
        self.llm_calls = 0
        self.llm_errors = 0
        self.error_kinds: dict[str, int] = {}
        self.quota_hit = False

    def record(self, verdict: dict) -> None:
        self.llm_calls += 1
        if "error" in verdict:
            self.llm_errors += 1
            kind = verdict.get("error_kind", "other")
            self.error_kinds[kind] = self.error_kinds.get(kind, 0) + 1

    @property
    def error_ratio(self) -> float:
        return self.llm_errors / self.llm_calls if self.llm_calls else 0.0

    @property
    def unhealthy(self) -> bool:
        return self.quota_hit or (
            self.llm_calls > 0 and self.error_ratio >= LLM_FAILURE_ABORT_RATIO
        )


def scan() -> int:
    print("MARKET INTELLIGENCE SCAN")
    _rule("=")

    seeded = seed_company_queue()
    if seeded:
        print(f"Queue seeded with {seeded} companies.")

    batch = get_next_company_batch(limit=BATCH_SIZE)
    if not batch:
        print("No companies in queue. Nothing to do.")
        return 0

    print(f"Scanning {len(batch)} companies:")
    for c in batch:
        print(f"  - {c['company_name']} ({c['ticker']}) / {c['domain']}")
    _rule()

    stats = ScanStats()
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
            result = gather_raw_items(ticker, name)
        except Exception as exc:
            print(f"  ! scrape failed: {exc}")
            traceback.print_exc()
            log_scan(ticker, name, domain, raw_items_count=0, signals_found=0,
                     raw_collected=0)
            processed_ids.append(cid)
            continue

        raw_items = result.items[:MAX_ITEMS_PER_COMPANY]
        company_signals = 0
        graded_items: list[dict] = []

        for j, item in enumerate(raw_items, start=1):
            total_items += 1
            snippet = f"{item.get('title', '')}\n{item.get('summary', '')}".strip()
            url = item.get("url", "")

            try:
                verdict = evaluate_item(name, domain, snippet, url)
            except LLMQuotaError as exc:
                stats.quota_hit = True
                print(f"  !! LLM quota exhausted: {exc}")
                print("  !! Stopping this run cleanly; unprocessed companies "
                      "keep their old last_scanned and are retried next cycle.")
                # Finalize just what completed so far.
                mark_items_seen(graded_items, company_symbol=ticker)
                log_scan(ticker, name, domain, raw_items_count=j - 1,
                         signals_found=company_signals, raw_collected=result.collected)
                processed_ids.append(cid)
                return _finalize(stats, processed_ids, total_items, total_signals,
                                 saved_signals, aborted=True)

            stats.record(verdict)
            # Only fingerprint items the LLM actually judged; a transient API
            # error leaves the URL unseen so the next run retries it.
            if "error" not in verdict:
                graded_items.append(item)
            score = verdict["conviction_score"]
            flag = "SIGNAL" if verdict["valid"] else (
                f"ERR:{verdict.get('error_kind', '?')}" if "error" in verdict
                else "filler"
            )
            print(
                f"    ({j}/{len(raw_items)}) [{score}/10 {flag}] "
                f"{item.get('title', '')[:70]}"
            )

            if verdict["valid"]:
                try:
                    written = save_signal(
                        company=name,
                        domain=domain,
                        score=score,
                        headline=verdict["headline"],
                        analysis=verdict["analysis"],
                        url=url,
                    )
                except Exception as exc:  # a DB write must not kill the scan
                    written = False
                    print(f"      ! save_signal failed: {exc}")
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

            time.sleep(ITEM_PACING_SECONDS)

        # Fingerprint every graded URL so it is never re-sent to the LLM.
        mark_items_seen(graded_items, company_symbol=ticker)

        # Audit-log this company's outcome immediately after it finishes.
        log_scan(
            company_symbol=ticker,
            company_name=name,
            domain=domain,
            raw_items_count=len(raw_items),
            signals_found=company_signals,
            raw_collected=result.collected,
        )

        print(f"  => {company_signals} high-conviction signal(s) from {name}.")
        processed_ids.append(cid)

    return _finalize(stats, processed_ids, total_items, total_signals,
                     saved_signals, aborted=False)


def _finalize(
    stats: ScanStats,
    processed_ids: list[int],
    total_items: int,
    total_signals: int,
    saved_signals: list[dict],
    aborted: bool,
) -> int:
    update_scan_timestamp(processed_ids)
    prune_scan_logs()
    prune_seen_urls()

    _rule("=")
    print("SCAN ABORTED (LLM quota)" if aborted else "SCAN COMPLETE")
    print(f"  Companies processed : {len(processed_ids)}")
    print(f"  Items graded        : {total_items}")
    print(f"  Signals saved (>=8) : {total_signals}")
    print(f"  LLM calls / errors  : {stats.llm_calls} / {stats.llm_errors} "
          f"({stats.error_ratio:.0%})")
    if stats.error_kinds:
        breakdown = ", ".join(f"{k}={v}" for k, v in sorted(stats.error_kinds.items()))
        print(f"  Error breakdown     : {breakdown}")

    if saved_signals:
        print("\n  Top signals:")
        for s in sorted(saved_signals, key=lambda x: -x["score"]):
            print(f"    [{s['score']}/10] {s['company']}: {s['headline']}")
    else:
        print("\n  No high-conviction breakthroughs this cycle.")

    if stats.unhealthy:
        _rule("!")
        if stats.quota_hit:
            print("  UNHEALTHY: LLM quota/rate limit exhausted this run.")
        if stats.llm_calls and stats.error_ratio >= LLM_FAILURE_ABORT_RATIO:
            print(f"  UNHEALTHY: {stats.error_ratio:.0%} of LLM calls failed "
                  f"(threshold {LLM_FAILURE_ABORT_RATIO:.0%}).")
            if stats.error_kinds.get("auth"):
                print("  -> auth errors dominate: check LLM_API_KEY in .env / "
                      "repo secrets (free Groq key: https://console.groq.com/keys).")
        _rule("!")
        return 1

    _rule("=")
    return 0


if __name__ == "__main__":
    sys.exit(scan())
