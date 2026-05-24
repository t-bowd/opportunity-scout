-- Migration 002: Paper trading tables
-- Run in the Supabase SQL editor before deploying the paper trader module.

-- Active and historical paper positions
CREATE TABLE IF NOT EXISTS paper_positions (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  opportunity_id    UUID REFERENCES opportunities(id),
  ticker            TEXT NOT NULL,
  pattern           TEXT NOT NULL,
  market            TEXT NOT NULL CHECK (market IN ('US', 'ASX')),

  -- Entry
  entry_price_usd   NUMERIC(12, 4) NOT NULL,   -- USD price incl. slippage
  entry_price_aud   NUMERIC(12, 4) NOT NULL,   -- AUD price incl. slippage + FX
  quantity          INT NOT NULL,
  brokerage_aud     NUMERIC(8, 2) NOT NULL DEFAULT 0,  -- $0 ASX, $1 US
  entry_date        DATE NOT NULL,
  entry_week_of     DATE NOT NULL,
  score_at_entry    INT NOT NULL,

  -- Status
  status            TEXT NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open', 'closed_time', 'closed_stop', 'closed_manual')),

  -- Exit (populated on close)
  exit_price_usd    NUMERIC(12, 4),
  exit_price_aud    NUMERIC(12, 4),
  exit_date         DATE,
  exit_reason       TEXT,
  pnl_aud           NUMERIC(10, 2),   -- (exit - entry) * qty - brokerage
  pnl_pct           NUMERIC(8, 2),    -- % return on entry_price_aud

  created_at        TIMESTAMPTZ DEFAULT NOW(),
  updated_at        TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS paper_positions_open_idx    ON paper_positions (status) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS paper_positions_ticker_idx  ON paper_positions (ticker);
CREATE INDEX IF NOT EXISTS paper_positions_entry_idx   ON paper_positions (entry_date DESC);

-- Daily portfolio snapshot for tracking expectancy over time
CREATE TABLE IF NOT EXISTS paper_portfolio_snapshots (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  snapshot_date       DATE NOT NULL UNIQUE,
  open_positions      INT NOT NULL DEFAULT 0,
  total_deployed_aud  NUMERIC(10, 2) NOT NULL DEFAULT 0,
  closed_trades       INT NOT NULL DEFAULT 0,
  winning_trades      INT NOT NULL DEFAULT 0,
  total_pnl_aud       NUMERIC(10, 2) NOT NULL DEFAULT 0,
  expectancy_aud      NUMERIC(10, 2),   -- avg P&L per closed trade
  win_rate            NUMERIC(5, 2),    -- % of winning trades
  created_at          TIMESTAMPTZ DEFAULT NOW()
);

-- Log of why an opportunity was not entered
CREATE TABLE IF NOT EXISTS paper_skipped_entries (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  opportunity_id    UUID REFERENCES opportunities(id),
  ticker            TEXT NOT NULL,
  score             INT NOT NULL,
  skip_reason       TEXT NOT NULL,
  week_of           DATE NOT NULL,
  created_at        TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS paper_skipped_week_idx ON paper_skipped_entries (week_of DESC);
