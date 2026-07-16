-- Small-cap catalyst sleeve — Phase 2 tables (SPEC §1).
-- Run once in Supabase (SQL editor). Entirely separate from the main book's
-- paper_* tables; nothing here references them.

create table if not exists smallcap_positions (
    id                  uuid primary key default gen_random_uuid(),
    ticker              text not null,
    name                text,
    exchange            text not null,              -- 'US' | 'ASX'
    vertical            text not null,              -- 'biotech' | 'asx_ann' | 'defense'
    catalyst            text,
    catalyst_date       date,                       -- null for non-dated (asx_ann)
    market_cap          numeric,
    entry_date          date not null,
    entry_price_native  numeric not null,
    entry_price_aud     numeric not null,
    quantity            integer not null,
    fx_rate             numeric,                    -- USD per AUD at entry
    brokerage_aud       numeric default 0,
    peak_price_aud      numeric,
    trailing_stop_active boolean default false,
    status              text not null default 'open',  -- 'open' | 'closed'
    exit_date           date,
    exit_price_aud      numeric,
    exit_reason         text,
    pnl_aud             numeric,
    pnl_pct             numeric,
    created_at          timestamptz default now()
);
create index if not exists smallcap_positions_status_idx on smallcap_positions (status);

create table if not exists smallcap_skipped (
    id          uuid primary key default gen_random_uuid(),
    ticker      text not null,
    vertical    text,
    reason      text,
    skip_date   date not null,
    created_at  timestamptz default now()
);

create table if not exists smallcap_snapshots (
    snapshot_date       date primary key,
    open_positions      integer,
    deployed_aud        numeric,
    unrealized_pnl_aud  numeric,
    closed_count        integer,
    realized_pnl_aud    numeric,
    win_rate            numeric,
    created_at          timestamptz default now()
);
