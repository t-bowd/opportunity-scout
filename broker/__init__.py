"""
Broker execution layer — Alpaca native orders for the US main-book positions.

Supabase stays the book of record (see broker/SPEC.md §2); this package is the
*executor*. It submits resting stop / trailing-stop orders to Alpaca so exits
fire intraday instead of on the once-a-day poll, and reconciles the resulting
fills back into Supabase.

Everything here fails soft: if the Alpaca credentials are absent the client is
disabled and the daily run behaves exactly as it did before the integration
(the ASX simulator and snapshot are untouched). ASX names never route here.
"""
