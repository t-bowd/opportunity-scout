"""
Sleeve portfolio snapshot — its OWN stats, entirely separate from the main book's
expectancy/graduation math (SPEC §1, §8).
"""

from datetime import date

from smallcap import store
from smallcap.pricing import current_price_native, to_aud


def run_snapshot() -> None:
    today = date.today()
    open_positions = store.get_open_positions()
    closed = store.get_closed_positions()

    deployed = 0.0
    unrealized = 0.0
    for p in open_positions:
        deployed += float(p["entry_price_aud"]) * p["quantity"]
        px = current_price_native(p["ticker"])
        if px is not None:
            price_aud = to_aud(px, p["exchange"], float(p.get("fx_rate") or 0.65))
            unrealized += (price_aud - float(p["entry_price_aud"])) * p["quantity"]

    realized = sum(float(p.get("pnl_aud") or 0) for p in closed)
    wins = sum(1 for p in closed if float(p.get("pnl_aud") or 0) > 0)
    win_rate = round(wins / len(closed), 3) if closed else 0.0

    store.upsert_snapshot({
        "snapshot_date": today.isoformat(),
        "open_positions": len(open_positions),
        "deployed_aud": round(deployed, 2),
        "unrealized_pnl_aud": round(unrealized, 2),
        "closed_count": len(closed),
        "realized_pnl_aud": round(realized, 2),
        "win_rate": win_rate,
    })
    print(f"[smallcap/snapshot] {len(open_positions)} open, ${deployed:.0f} deployed, "
          f"{unrealized:+.2f} unrealized | {len(closed)} closed, "
          f"${realized:+.2f} realized, win rate {win_rate:.0%}")
