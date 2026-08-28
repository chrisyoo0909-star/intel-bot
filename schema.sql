-- Market intelligence pipeline — full Supabase schema.
-- Idempotent: safe to run repeatedly. Run in the Supabase SQL editor.
-- The `alter ... add column if not exists` lines below bring pre-existing
-- tables (created by an earlier version) up to the current shape.

-- ── company_queue ─────────────────────────────────────────────────────────
create table if not exists company_queue (
    id            bigint generated always as identity primary key,
    ticker        text not null unique,
    company_name  text not null,
    domain        text not null,
    last_scanned  timestamptz not null default '1970-01-01T00:00:00Z'
);
alter table company_queue add column if not exists company_name text;
alter table company_queue add column if not exists domain       text;
alter table company_queue add column if not exists last_scanned timestamptz
    not null default '1970-01-01T00:00:00Z';

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
alter table signals add column if not exists company          text;
alter table signals add column if not exists domain           text;
alter table signals add column if not exists conviction_score int;
alter table signals add column if not exists headline         text;
alter table signals add column if not exists analysis         text;
alter table signals add column if not exists url              text;
alter table signals add column if not exists created_at       timestamptz not null default now();

-- Supply-Side Equity & Capacity Analyst fields.
alter table signals add column if not exists recommendation          text;
alter table signals add column if not exists price_target_delta_pct  numeric;
alter table signals add column if not exists implied_price_target    numeric;
alter table signals add column if not exists supply_chain_driver     text;
alter table signals add column if not exists financial_impact_thesis text;

-- Collapse pre-existing duplicates (keep the newest row) before adding the
-- unique constraints save_signal() upserts against.
delete from signals s using signals d
 where s.company = d.company and s.headline = d.headline and s.id < d.id;
delete from signals s using signals d
 where s.url = d.url and s.url is not null and s.id < d.id;

alter table signals drop constraint if exists signals_company_headline_key;
alter table signals add  constraint signals_company_headline_key
    unique (company, headline);
alter table signals drop constraint if exists signals_url_key;
alter table signals add  constraint signals_url_key unique (url);

-- ── scan_logs (audit trail, FIFO-pruned by the bot) ───────────────────────
create table if not exists scan_logs (
    id                uuid primary key default gen_random_uuid(),
    company_symbol    text,
    company_name      text,
    domain            text,
    raw_collected      int  not null default 0,  -- items found before URL dedup
    raw_items_count    int  not null default 0,  -- items actually sent to the LLM
    signals_found      int  not null default 0,
    dropped_unresolved int  not null default 0,  -- google redirect not resolvable
    dropped_paywall    int  not null default 0,  -- paywall / teaser body
    dropped_short      int  not null default 0,  -- body under MIN_BODY chars
    scanned_at         timestamptz not null default now()
);
alter table scan_logs add column if not exists company_symbol     text;
alter table scan_logs add column if not exists company_name       text;
alter table scan_logs add column if not exists domain             text;
alter table scan_logs add column if not exists raw_collected      int not null default 0;
alter table scan_logs add column if not exists raw_items_count    int not null default 0;
alter table scan_logs add column if not exists signals_found      int not null default 0;
alter table scan_logs add column if not exists dropped_unresolved int not null default 0;
alter table scan_logs add column if not exists dropped_paywall    int not null default 0;
alter table scan_logs add column if not exists dropped_short      int not null default 0;
alter table scan_logs add column if not exists scanned_at         timestamptz not null default now();
create index if not exists scan_logs_scanned_at_idx
    on scan_logs (scanned_at desc);

-- ── seen_urls (dedup: URLs already sent through the LLM) ──────────────────
create table if not exists seen_urls (
    url_hash        text primary key,
    url             text,
    company_symbol  text,
    first_seen_at   timestamptz not null default now()
);
alter table seen_urls add column if not exists url            text;
alter table seen_urls add column if not exists company_symbol text;
alter table seen_urls add column if not exists first_seen_at  timestamptz not null default now();
create index if not exists seen_urls_first_seen_at_idx
    on seen_urls (first_seen_at desc);

-- ── scan_state (single-row advisory lock: no two scans run concurrently) ──
create table if not exists scan_state (
    id            text primary key,
    locked_at     timestamptz,
    locked_until  timestamptz not null default '1970-01-01T00:00:00Z',
    host          text
);
alter table scan_state add column if not exists locked_at    timestamptz;
alter table scan_state add column if not exists locked_until timestamptz not null default '1970-01-01T00:00:00Z';
alter table scan_state add column if not exists host         text;
insert into scan_state (id) values ('singleton') on conflict (id) do nothing;

-- Force PostgREST to refresh its schema cache so the API sees new columns
-- immediately (otherwise a PGRST204 "column not found" can linger ~seconds).
notify pgrst, 'reload schema';
