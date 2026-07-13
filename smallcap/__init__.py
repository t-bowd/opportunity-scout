"""
Small-cap catalyst sleeve (Phase 1 — read-only screener).

A walled-off, catalyst-driven small-cap watchlist generator, separate from the
main insider-buy paper book. It pairs a small-cap ticker universe (US + ASX)
with free, structured catalyst feeds, and surfaces names that have a near-term
or just-hit catalyst AND are already climbing on price.

Phase 1 is READ-ONLY: it prints a ranked watchlist. No paper positions are
opened. See memory `smallcap-catalyst-sleeve` for the design and rationale.
"""
