"""
Computes and saves a daily portfolio snapshot.
Runs after exits have been processed.
"""

from datetime import date

from db.client import (
    get_open_paper_positions,
    get_closed_paper_positions,
    upsert_paper_snapshot,
)


def run_snapshot() -> None:
    today = date.today()
    open_pos = get_open_paper_positions()
    closed_pos = get_closed_paper_positions()

    deployed_aud = sum(
        float(p["entry_price_aud"]) * p["quantity"] + float(p.get("brokerage_aud", 0))
        for p in open_pos
    )

    closed_pnls = [
        float(p["pnl_aud"]) for p in closed_pos if p.get("pnl_aud") is not None
    ]
    total_pnl = sum(closed_pnls)
    wins = sum(1 for pnl in closed_pnls if pnl > 0)

    expectancy = total_pnl / len(closed_pnls) if closed_pnls else None
    win_rate = wins / len(closed_pnls) * 100 if closed_pnls else None

    snap = {
        "snapshot_date": today.isoformat(),
        "open_positions": len(open_pos),
        "total_deployed_aud": round(deployed_aud, 2),
        "closed_trades": len(closed_pnls),
        "winning_trades": wins,
        "total_pnl_aud": round(total_pnl, 2),
        "expectancy_aud": round(expectancy, 2) if expectancy is not None else None,
        "win_rate": round(win_rate, 2) if win_rate is not None else None,
    }

    upsert_paper_snapshot(snap)

    trades_needed = max(0, 20 - len(closed_pnls))
    exp_str = f"${expectancy:+.2f}" if expectancy is not None else "n/a"
    wr_str = f"{win_rate:.0f}%" if win_rate is not None else "n/a"

    print(
        f"[paper/snapshot] {len(open_pos)} open, "
        f"${deployed_aud:.0f} deployed, "
        f"{len(closed_pnls)} closed "
        f"(expectancy {exp_str}, win rate {wr_str}), "
        f"{trades_needed} trades until graduation review"
    )
