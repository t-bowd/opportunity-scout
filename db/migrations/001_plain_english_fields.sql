-- Migration 001: Add plain English explainer fields to opportunities table
-- Run this in the Supabase SQL editor.

ALTER TABLE opportunities
  ADD COLUMN IF NOT EXISTS plain_english TEXT,
  ADD COLUMN IF NOT EXISTS signal_type_explainer TEXT;
