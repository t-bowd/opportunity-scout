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

-- Per-position daily momentum observations. The exit run already computes the
-- "climbing / NOT climbing" verdict per open position each day but only prints
-- it — this persists it so the 4-6 week review can test a momentum-invalidation
-- exit: it needs to know WHEN a name stopped climbing relative to its peak, not
-- just where it peaked. One row per open position per run; the unique
-- (position_id, obs_date) makes a same-day re-run idempotent. Instrumentation
-- only — nothing here feeds an exit decision.
create table if not exists smallcap_momentum (
    id             uuid primary key default gen_random_uuid(),
    position_id    uuid not null references smallcap_positions(id) on delete cascade,
    ticker         text not null,
    obs_date       date not null,
    climbing       boolean,        -- null if price/momentum data was unavailable that day
    ret_20d        numeric,        -- 20-session % return (the momentum measure)
    pnl_pct        numeric,        -- position P&L% that day, for peak/fade context
    peak_gain_pct  numeric,        -- gain of the running peak vs entry, that day
    created_at     timestamptz default now(),
    unique (position_id, obs_date)
);
create index if not exists smallcap_momentum_position_idx on smallcap_momentum (position_id);
