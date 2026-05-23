"""
Auto-updates price_30d / price_90d on feedback rows.
Run weekly — checks any opportunity that's 30+ or 90+ days old.
"""
import os
import requests
from datetime import date, timedelta
from db.client import get_feedback_pending_pnl, update_feedback_pnl


def _get_price(ticker: str) -> float | None:
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=5d"
        resp = requests.get(url, headers={"User-Agent": "OpportunityScout"}, timeout=10)
        return resp.json()["chart"]["result"][0]["meta"]["regularMarketPrice"]
    except Exception:
        return None


def run() -> None:
    rows = get_feedback_pending_pnl()
    today = date.today()
    updated = 0

    for row in rows:
        opp = row.get("opportunities", {})
        ticker = opp.get("vehicle")
        created_raw = opp.get("created_at", "")
        if not ticker or not created_raw:
            continue

        created = date.fromisoformat(created_raw[:10])
        age_days = (today - created).days
        updates = {}

        if age_days >= 30 and row.get("price_30d") is None:
            price = _get_price(ticker)
            if price:
                updates["price_30d"] = price

        if age_days >= 90 and row.get("price_90d") is None:
            price = _get_price(ticker)
            if price:
                updates["price_90d"] = price

        if updates:
            updates["updated_at"] = "now()"
            update_feedback_pnl(row["id"], updates)
            updated += 1

    print(f"[pnl_tracker] updated {updated} feedback rows")


if __name__ == "__main__":
    run()
