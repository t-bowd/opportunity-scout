"""Entry point for the daily GitHub Actions workflow."""
import os
from dotenv import load_dotenv

load_dotenv(".env.local")

from collectors import edgar, etf_launches, news
from analyzer import classify, score

print("=== Daily collection run ===")

edgar.collect(lookback_days=7)
etf_launches.collect()
news.collect()

print("\n=== Classifying signals ===")
classify.run()

print("\n=== Scoring opportunities ===")
score.score_week()

print("\n=== Done ===")
