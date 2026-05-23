-- Run this once in the Supabase SQL editor to initialize the schema.

CREATE TABLE entities (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  ticker       TEXT,
  name         TEXT NOT NULL,
  type         TEXT NOT NULL CHECK (type IN ('stock', 'etf', 'private', 'spac')),
  notes        TEXT,
  watchlisted  BOOLEAN DEFAULT false,
  created_at   TIMESTAMPTZ DEFAULT NOW()
);

CREATE UNIQUE INDEX entities_ticker_idx ON entities (ticker) WHERE ticker IS NOT NULL;

CREATE TABLE signals (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  source       TEXT NOT NULL,
  -- 'edgar_s1' | 'edgar_n1a' | 'edgar_13f' | 'edgar_13d' | 'edgar_form4' | 'news' | 'etf_launch'
  entity_id    UUID REFERENCES entities(id),
  accession_no TEXT UNIQUE,  -- EDGAR accession number; used for dedup
  raw_data     JSONB NOT NULL,
  summary      TEXT,         -- Gemini-generated
  pattern      TEXT,         -- 'pre_ipo_proxy' | 'thematic_etf' | 'insider_buy' | 'activist' | 's1_filed'
  signal_date  DATE NOT NULL,
  url          TEXT,
  processed    BOOLEAN DEFAULT false,
  created_at   TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX signals_processed_idx ON signals (processed) WHERE processed = false;
CREATE INDEX signals_date_idx ON signals (signal_date DESC);

CREATE TABLE opportunities (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  entity_id        UUID REFERENCES entities(id),
  title            TEXT NOT NULL,
  thesis           TEXT NOT NULL,   -- Gemini-generated reasoning
  vehicle          TEXT NOT NULL,   -- the ticker to actually trade
  pattern          TEXT NOT NULL,
  catalyst         TEXT,
  invalidation     TEXT,
  conviction       INT CHECK (conviction BETWEEN 0 AND 5),
  asymmetry        INT CHECK (asymmetry BETWEEN 0 AND 5),
  liquidity        INT CHECK (liquidity BETWEEN 0 AND 5),
  timing           INT CHECK (timing BETWEEN 0 AND 5),
  total_score      INT GENERATED ALWAYS AS (conviction + asymmetry + liquidity + timing) STORED,
  price_at_score   NUMERIC(12, 4),
  catalyst_date    DATE,
  signal_ids       UUID[],
  week_of          DATE NOT NULL,   -- Monday of the ISO week
  created_at       TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX opportunities_week_idx ON opportunities (week_of DESC);
CREATE INDEX opportunities_score_idx ON opportunities (total_score DESC);

CREATE TABLE feedback (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  opportunity_id  UUID REFERENCES opportunities(id) UNIQUE,
  acted           BOOLEAN,
  grade           INT CHECK (grade BETWEEN 1 AND 5),
  notes           TEXT,
  entry_price     NUMERIC(12, 4),
  price_30d       NUMERIC(12, 4),   -- auto-populated by cron
  price_90d       NUMERIC(12, 4),   -- auto-populated by cron
  pnl_30d_pct     NUMERIC(8, 2)
                    GENERATED ALWAYS AS (
                      CASE WHEN entry_price > 0 AND price_30d IS NOT NULL
                      THEN ROUND(((price_30d - entry_price) / entry_price) * 100, 2)
                      ELSE NULL END
                    ) STORED,
  pnl_90d_pct     NUMERIC(8, 2)
                    GENERATED ALWAYS AS (
                      CASE WHEN entry_price > 0 AND price_90d IS NOT NULL
                      THEN ROUND(((price_90d - entry_price) / entry_price) * 100, 2)
                      ELSE NULL END
                    ) STORED,
  created_at      TIMESTAMPTZ DEFAULT NOW(),
  updated_at      TIMESTAMPTZ DEFAULT NOW()
);
