"""
One-off backfill: give every past scored opportunity a feedback row and fill its
30d / 90d forward price from HISTORICAL data.

Why historical (not the weekly pnl_tracker): pnl_tracker stamps the *current*
market price, which is only ~right for a pick that just crossed 30 days. Run over
old opportunities it would record today's price as "price_30d" — wrong. Here we
fetch the actual daily close nearest to (scored_date + 30d) and (+90d).

After this runs once, the score.py wiring keeps new picks covered going forward,
and pnl_tracker maintains them weekly. Safe to re-run: feedback upsert is
idempotent and we only fill price_30d/price_90d cells that are still empty.

    python run_backfill_feedback.py            # do the backfill
    python run_backfill_feedback.py --dry-run  # report what it would fill, write nothing
"""
import sys
import time
import requests
from datetime import date, timedelta, datetime, timezone

from db.client import (
    get_client,
    insert_feedback_rows,
    get_feedback_pending_pnl,
    update_feedback_pnl,
)

DRY_RUN = "--dry-run" in sys.argv


def _hist_close(ticker: str, target: date) -> float | None:
    """Daily close nearest to `target` (within a ~5-day window either side).

    Picks the session closest to the target date, so weekends/holidays around the
    30/90-day mark still resolve to a real traded price.
    """
    t1 = int(datetime.combine(target - timedelta(days=6), datetime.min.time(),
                              tzinfo=timezone.utc).timestamp())
    t2 = int(datetime.combine(target + timedelta(days=6), datetime.min.time(),
                              tzinfo=timezone.utc).timestamp())
    try:
        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
            f"?period1={t1}&period2={t2}&interval=1d"
        )
        resp = requests.get(url, headers={"User-Agent": "OpportunityScout"}, timeout=15)
        result = resp.json()["chart"]["result"][0]
        stamps = result["timestamp"]
        closes = result["indicators"]["quote"][0]["close"]
    except Exception:
        return None

    best, best_gap = None, None
    for ts, c in zip(stamps, closes):
        if c is None:
            continue
        d = datetime.fromtimestamp(ts, tz=timezone.utc).date()
        gap = abs((d - target).days)
        if best_gap is None or gap < best_gap:
            best, best_gap = c, gap
    return round(best, 4) if best is not None else None


def run() -> None:
    db = get_client()

    # 1. Every scored opportunity gets a feedback row (idempotent upsert).
    opps = db.table("opportunities").select("id").execute().data
    ids = [o["id"] for o in opps]
    print(f"[backfill] {len(ids)} opportunities total")
    if not DRY_RUN and ids:
        for i in range(0, len(ids), 500):
            insert_feedback_rows(ids[i:i + 500])
    print(f"[backfill] feedback rows ensured{' (dry-run: skipped)' if DRY_RUN else ''}")

    # 2. Fill 30d / 90d historical closes for rows old enough and not yet filled.
    today = date.today()
    rows = get_feedback_pending_pnl()
    filled_30 = filled_90 = skipped = 0

    for row in rows:
        opp = row.get("opportunities") or {}
        ticker = opp.get("vehicle")
        created_raw = opp.get("created_at", "")
        if not ticker or not created_raw:
            skipped += 1
            continue
        created = date.fromisoformat(created_raw[:10])
        age = (today - created).days
        updates = {}

        if age >= 30 and row.get("price_30d") is None:
            p = _hist_close(ticker, created + timedelta(days=30))
            if p:
                updates["price_30d"] = p
                filled_30 += 1

        if age >= 90 and row.get("price_90d") is None:
            p = _hist_close(ticker, created + timedelta(days=90))
            if p:
                updates["price_90d"] = p
                filled_90 += 1

        if updates:
            ref = opp.get("price_at_score")
            chg = ""
            if ref and updates.get("price_30d"):
                chg = f"  30d: {ticker} {ref}→{updates['price_30d']} ({(updates['price_30d']/float(ref)-1)*100:+.1f}%)"
            print(f"[backfill] {ticker} age={age}d{chg}")
            if not DRY_RUN:
                updates["updated_at"] = "now()"
                update_feedback_pnl(row["id"], updates)
            time.sleep(0.2)  # be gentle on Yahoo

    print(f"[backfill] done{' (dry-run)' if DRY_RUN else ''} — "
          f"filled {filled_30} × 30d, {filled_90} × 90d, {skipped} skipped (no ticker/date)")


if __name__ == "__main__":
    run()
