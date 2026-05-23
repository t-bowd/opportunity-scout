"""Entry point for the weekly digest GitHub Actions workflow."""
import os
from dotenv import load_dotenv

load_dotenv(".env.local")

from analyzer.pnl_tracker import run as update_pnl
from digest.weekly_report import send as send_digest

print("=== Updating P&L on old opportunities ===")
update_pnl()

print("\n=== Sending weekly digest ===")
send_digest()

print("\n=== Done ===")
