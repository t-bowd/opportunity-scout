-- Migration 005: Instrument two candidate loser-signals on scored opportunities
-- Run in the Supabase SQL editor. MUST be applied before the score.py + run_analysis.py
-- changes in the same commit deploy (the analysis query selects these columns).
--
-- Why: the forward-return analysis (n=150) shows the SCORED attributes (score,
-- conviction/asymmetry/liquidity/timing) barely predict which picks lose — so the
-- live stops can't be gated from them. The two remaining, mechanism-grounded
-- hypotheses are NOT in the feature set yet, so we start persisting them now and
-- test at a later analysis run (instrument-now / analyze-later, like the sleeve's
-- momentum capture):
--
--   rescore_action    — how today's score related to the ticker's most recent
--                       prior score: 'insert' (new) | 'rescore_up' | 'rescore_down'.
--                       Hypothesis: a name RE-SCORED DOWN on its entry day is already
--                       fading and underperforms (REZI, the fastest live stop, was
--                       18->16 at entry). Computed today but only printed, never saved.
--   ret_20d_at_score  — trailing 20-trading-day price return at score time (a price-
--                       path feature ORTHOGONAL to the fundamental score dimensions).
--                       Hypothesis: names bought already extended (big prior run-up)
--                       revert — the sleeve's clearest finding (GES/JSPR). Free to
--                       compute: the scorer already fetches the 1y daily closes.
--
-- Both nullable; every historical row stays NULL (no reliable backfill), so the
-- analysis segments on them only as data accrues going forward.

ALTER TABLE opportunities
  ADD COLUMN IF NOT EXISTS rescore_action   TEXT,            -- 'insert' | 'rescore_up' | 'rescore_down'
  ADD COLUMN IF NOT EXISTS ret_20d_at_score NUMERIC(8, 4);   -- trailing 20-session return, e.g. 0.1240 = +12.4%
