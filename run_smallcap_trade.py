"""
Phase 2 entry point — small-cap catalyst PAPER TRADING.

Walled off from the main book (SPEC §1): own smallcap_* tables, no imports from
paper_trader, own hard $1,000 pool, own stats. Exits run before entries (a name
freed today can be re-used), mirroring the main book's ordering only in sequence,
not in code.

Needs SUPABASE_URL + SUPABASE_SERVICE_KEY (the smallcap_* tables must exist —
see smallcap/schema.sql). Run daily via .github/workflows/smallcap-trade.yml.
"""

import os
from dotenv import load_dotenv

load_dotenv(".env.local")

from smallcap import exit as sc_exit
from smallcap import entry as sc_entry
from smallcap import snapshot as sc_snapshot

print("=== Small-cap sleeve — paper trading run ===")

print("\n--- exits ---")
sc_exit.run_exits()

print("\n--- entries ---")
sc_entry.run_entries()

print("\n--- snapshot ---")
sc_snapshot.run_snapshot()

print("\n=== Done ===")
