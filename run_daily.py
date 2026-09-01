"""Entry point for the daily GitHub Actions workflow."""
import os
from datetime import date

from dotenv import load_dotenv

load_dotenv(".env.local")

from collectors import edgar, etf_launches, news
from analyzer import classify, score
from paper_trader import entry as paper_entry
from paper_trader import exit as paper_exit
from paper_trader import snapshot as paper_snapshot
from broker import reconcile as broker_reconcile
from db.client import get_latest_paper_snapshot


def _already_collected_today() -> bool:
    """True if a full daily run already finished today (today's snapshot exists).

    The workflow fires three times a day for REDUNDANCY against GitHub's late/dropped
    cron (see daily.yml) — the extra runs exist to give deferred live entries another
    shot inside market hours, NOT to re-collect. So on a second/third run we skip the
    expensive, external, once-a-day work (EDGAR/news pulls + Gemini scoring) but STILL
    run the cheap, idempotent trade stages below. The snapshot is written at the end of
    a run, so its presence for today means collection+scoring already succeeded today.
    Order-independent: whichever run lands first does the collect+score."""
    snap = get_latest_paper_snapshot()
    return bool(snap and snap.get("snapshot_date") == date.today().isoformat())


print("=== Daily collection run ===")

if _already_collected_today():
    print("[run_daily] today's collection + scoring already done — "
          "skipping collect/score, running trade stages only (redundant in-window run).")
else:
    edgar.collect(lookback_days=3)
    etf_launches.collect()
    news.collect()

    print("\n=== Classifying signals ===")
    classify.run()

    print("\n=== Scoring opportunities ===")
    score.score_week()

# Trade stages ALWAYS run — they're cheap and idempotent (reconcile reads fills,
# exits skip already-closed rows, entries dedupe via ticker_already_open + the live
# position cap, snapshot upserts by date). Re-running them is how a later in-window
# run fills the live slots an earlier out-of-window run had to defer.
print("\n=== Broker — reconcile fills ===")
broker_reconcile.run()

print("\n=== Paper trading — exits ===")
paper_exit.run_exits()

print("\n=== Paper trading — entries ===")
paper_entry.run_entries()

print("\n=== Paper trading — snapshot ===")
paper_snapshot.run_snapshot()

print("\n=== Done ===")
