-- Migration 006: Per-position volatility-scaled stop distance
-- Run in the Supabase SQL editor before deploying the entry/exit changes.
--
-- Until now every position used the same flat -12% stop, regardless of how much
-- the name actually moves. On a 30-60 day hold that line sits INSIDE ordinary
-- noise for a volatile name: at ~3.5% daily vol the 60-day sigma is ~27%, so 12%
-- is ~0.45 sigma and a driftless walk touches it roughly two thirds of the time.
-- That is the mechanism behind six-for-six live stop-outs at a median of 12 days,
-- on signals whose edge only shows up around day 30-60 — positions were ejected
-- before the thesis could play out.
--
-- stop_loss_pct stores the POSITIVE stop distance chosen for that position at
-- entry (e.g. 17.5 = "exit if down 17.5%"), derived from the name's own ATR. It is
-- written once at entry and never revised, so the exit poll and the broker's
-- resting order always agree on the same line, and a position's risk terms can't
-- silently change underneath it mid-hold.
--
-- Nullable on purpose: every EXISTING open position stays NULL and keeps falling
-- back to the flat -12%, so the current cohort is untouched and closes under the
-- rules it was opened with (which also keeps the graduation re-review sample
-- clean — entry_date tells you which regime a close belongs to).

ALTER TABLE paper_positions
  ADD COLUMN IF NOT EXISTS stop_loss_pct NUMERIC(6, 3);  -- positive % distance, NULL = legacy flat stop
