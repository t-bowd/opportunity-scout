-- Migration 004: Broker (Alpaca) execution bookkeeping on paper positions
-- Run in the Supabase SQL editor before deploying the broker/ integration.
--
-- All columns are nullable and default to the simulator's behaviour, so ASX
-- names and every position opened before cutover keep working unchanged. Only
-- US positions opened through Alpaca carry 'alpaca' + the order ids.

ALTER TABLE paper_positions
  ADD COLUMN IF NOT EXISTS broker               TEXT DEFAULT 'sim',  -- 'sim' | 'alpaca'
  ADD COLUMN IF NOT EXISTS broker_order_id      TEXT,   -- the entry BUY order
  ADD COLUMN IF NOT EXISTS broker_exit_order_id TEXT;   -- current resting stop / trailing / time-exit SELL

CREATE INDEX IF NOT EXISTS paper_positions_broker_open_idx
  ON paper_positions (broker) WHERE status = 'open';
