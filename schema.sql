-- Market intelligence pipeline — full Supabase schema.
-- Safe to run more than once. Run in the Supabase SQL editor.

-- ── company_queue ─────────────────────────────────────────────────────────
create table if not exists company_queue (
    id            bigint generated always as identity primary key,
    ticker        text not null unique,
    company_name  text not null,
    domain        text not null,
    last_scanned  timestamptz not null default '1970-01-01T00:00:00Z'
);

-- ── signals ───────────────────────────────────────────────────────────────
create table if not exists signals (
    id                bigint generated always as identity primary key,
    company           text not null,
    domain            text not null,
    conviction_score  int  not null,
    headline          text not null,
    analysis          text not null,
    url               text,
    created_at        timestamptz not null default now()
);

-- Collapse any pre-existing duplicates, keeping the newest row per story ...
delete from signals s
using signals d
where s.company = d.company
  and s.headline = d.headline
  and s.id < d.id;

-- ... then enforce uniqueness so save_signal() can upsert instead of insert.
alter table signals drop constraint if exists signals_company_headline_key;
alter table signals add  constraint signals_company_headline_key
    unique (company, headline);

-- ── scan_logs (audit trail, FIFO-pruned by the bot) ───────────────────────
create table if not exists scan_logs (
    id                uuid primary key default gen_random_uuid(),
    company_symbol    text,
    company_name      text,
    domain            text,
    raw_items_count   int  not null default 0,
    signals_found     int  not null default 0,
    scanned_at        timestamptz not null default now()
);
create index if not exists scan_logs_scanned_at_idx
    on scan_logs (scanned_at desc);

-- ── seen_urls (dedup: URLs already sent through Gemini) ────────────────────
create table if not exists seen_urls (
    url_hash        text primary key,
    url             text,
    company_symbol  text,
    first_seen_at   timestamptz not null default now()
);
create index if not exists seen_urls_first_seen_at_idx
    on seen_urls (first_seen_at desc);
