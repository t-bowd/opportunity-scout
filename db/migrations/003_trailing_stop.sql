-- Migration 003: Trailing stop support on paper positions
-- Run in the Supabase SQL editor.

ALTER TABLE paper_positions
  ADD COLUMN IF NOT EXISTS peak_price_aud  NUMERIC(12, 4),
  ADD COLUMN IF NOT EXISTS trailing_stop_active BOOLEAN DEFAULT false;

-- Extend the status check to include trailing stop closes
ALTER TABLE paper_positions DROP CONSTRAINT IF EXISTS paper_positions_status_check;
ALTER TABLE paper_positions ADD CONSTRAINT paper_positions_status_check
  CHECK (status IN ('open', 'closed_time', 'closed_stop', 'closed_trail', 'closed_manual'));
