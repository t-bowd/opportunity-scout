"""Entry point for the daily GitHub Actions workflow."""
import os
from dotenv import load_dotenv

load_dotenv(".env.local")

from collectors import edgar, etf_launches, news
from analyzer import classify, score
from paper_trader import entry as paper_entry
from paper_trader import exit as paper_exit
from paper_trader import snapshot as paper_snapshot

print("=== Daily collection run ===")

edgar.collect(lookback_days=3)
etf_launches.collect()
news.collect()

print("\n=== Classifying signals ===")
classify.run()

print("\n=== Scoring opportunities ===")
score.score_week()

print("\n=== Paper trading — exits ===")
paper_exit.run_exits()

print("\n=== Paper trading — entries ===")
paper_entry.run_entries()

print("\n=== Paper trading — snapshot ===")
paper_snapshot.run_snapshot()

print("\n=== Done ===")
