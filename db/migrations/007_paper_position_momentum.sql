-- Migration 007: Daily per-position momentum capture for the MAIN book
-- Run in the Supabase SQL editor before deploying the exit.py change.
--
-- Instrumentation only. Nothing reads this table yet and no exit rule consults it
-- — it exists so that in ~6-8 weeks the main book can be asked the same questions
-- the sleeve was just able to answer from its own two months of quiet recording.
--
-- Why now: the sleeve's momentum capture (smallcap_momentum, live since
-- 2026-08-10) is the only reason run_smallcap_analysis.py could test a
-- momentum-invalidation exit against real outcomes instead of guessing. The main
-- book has no equivalent — it records peak and the feedback table's 30/90d forward
-- returns, but nothing about whether a position was still climbing day to day. So
-- the same question cannot currently be asked of real money at all.
--
-- Deliberately mirrors smallcap_momentum's shape so the two books' analyses stay
-- directly comparable.

create table if not exists paper_position_momentum (
    id             uuid primary key default gen_random_uuid(),
    position_id    uuid not null references paper_positions(id) on delete cascade,
    ticker         text not null,
    obs_date       date not null,
    climbing       boolean,        -- null when price/momentum data was unavailable
    ret_20d        numeric,        -- 20-session % return (the momentum measure)
    ret_5d         numeric,
    pnl_pct        numeric,        -- position P&L% that day, for peak/fade context
    peak_gain_pct  numeric,        -- gain of the running peak vs entry, that day
    created_at     timestamptz default now(),
    unique (position_id, obs_date)   -- idempotent: a same-day re-run upserts
);

create index if not exists paper_position_momentum_position_idx
    on paper_position_momentum (position_id);
