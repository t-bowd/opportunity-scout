"""
Entry point for the hourly EDGAR watcher.

Collects the most recent filings and classifies them early so they're ready for
the next daily scoring run. Per-filing alert emails were removed — every S-1 IPO
registration was emailing an alert, which is noise now that trading is automated.
Notifications fire on actual position opens/closes instead (paper_trader.notify).
"""
import time
from dotenv import load_dotenv

load_dotenv(".env.local")

from collectors import edgar
from analyzer.classify import classify_signal
from db.client import get_unprocessed_signals, mark_signal_processed


def run() -> None:
    edgar.collect(lookback_days=1)  # EDGAR API has no sub-day filter; dedup handles overlap

    signals = get_unprocessed_signals(limit=20)
    for signal in signals:
        result = classify_signal(signal)
        if not result:
            continue
        mark_signal_processed(
            signal["id"], result.get("summary", ""), result.get("pattern", "irrelevant")
        )
        time.sleep(4)  # Gemini rate limit (news signals only)


if __name__ == "__main__":
    run()
