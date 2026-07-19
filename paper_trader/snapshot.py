"""
Computes and saves a daily portfolio snapshot.
Runs after exits have been processed.
"""

import bisect
from datetime import date, datetime, timezone

import requests

from db.client import (
    get_open_paper_positions,
    get_closed_paper_positions,
    upsert_paper_snapshot,
)
from paper_trader.exit import _fetch_price, _fetch_fx_rate


def _daily_closes(symbol: str) -> list[tuple[date, float]]:
    """Sorted (date, close) for the last year, dropping gap days. [] on failure."""
    try:
        url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
               "?interval=1d&range=1y")
        resp = requests.get(url, headers={"User-Agent": "OpportunityScout"}, timeout=10)
        result = resp.json()["chart"]["result"][0]
        ts = result["timestamp"]
        closes = result["indicators"]["quote"][0]["close"]
        hist = [(datetime.fromtimestamp(t, tz=timezone.utc).date(), c)
                for t, c in zip(ts, closes) if c is not None]
        hist.sort()
        return hist
    except Exception:
        return []


def _close_on_or_before(hist: list[tuple[date, float]], target: date) -> float | None:
    """The close on `target`, or the most recent trading day before it."""
    if not hist:
        return None
    i = bisect.bisect_right([d for d, _ in hist], target) - 1
    return hist[i][1] if i >= 0 else None


def _spy_benchmark(open_pos: list[dict], fx_rate: float) -> str | None:
    """
    Capital-weighted "vs market" line for the OPEN book: the book's mark-to-market
    AUD return against an AUD investor who'd instead bought SPY on each position's
    own entry date. FX is applied to the SPY leg too (AUD->USD in, USD->AUD out),
    so US names and their SPY counterfactual carry the same currency move and the
    comparison is apples-to-apples. Returns a printable line, or None if data is
    thin.
    """
    if not open_pos:
        return None
    spy_hist = _daily_closes("SPY")
    audusd_hist = _daily_closes("AUDUSD=X")
    spy_now = _fetch_price("SPY")
    if not spy_hist or not spy_now or not fx_rate:
        return None

    cost_sum = 0.0        # AUD cost basis (weight)
    value_sum = 0.0       # AUD current mark
    spy_value_sum = 0.0   # AUD value if that cost had gone into SPY at entry
    for pos in open_pos:
        entry_date = date.fromisoformat(pos["entry_date"])
        qty = pos["quantity"]
        cost_aud = float(pos["entry_price_aud"]) * qty
        price = _fetch_price(pos["ticker"])
        spy_entry = _close_on_or_before(spy_hist, entry_date)
        audusd_entry = _close_on_or_before(audusd_hist, entry_date)
        if not price or not spy_entry or not audusd_entry or cost_aud <= 0:
            continue  # exclude from BOTH legs so weights stay aligned

        if pos["ticker"].endswith(".AX"):
            value_aud = price * qty
        else:
            value_aud = price / fx_rate * qty   # USD -> AUD, matches exit.py

        # AUD investor's SPY return over the same window (see docstring).
        spy_ret = (spy_now / spy_entry) * (audusd_entry / fx_rate)

        cost_sum += cost_aud
        value_sum += value_aud
        spy_value_sum += cost_aud * spy_ret

    if cost_sum <= 0:
        return None
    book_pct = (value_sum / cost_sum - 1) * 100
    spy_pct = (spy_value_sum / cost_sum - 1) * 100
    return (f"[paper/snapshot] open book {book_pct:+.1f}% vs SPY {spy_pct:+.1f}% "
            f"(same entries, AUD) -> alpha {book_pct - spy_pct:+.1f}%")


def run_snapshot() -> None:
    today = date.today()
    open_pos = get_open_paper_positions()
    closed_pos = get_closed_paper_positions()

    deployed_aud = sum(
        float(p["entry_price_aud"]) * p["quantity"] + float(p.get("brokerage_aud", 0))
        for p in open_pos
    )

    # Manual closes are interventions (e.g. flushing a position opened on a
    # mislabeled signal), not strategy outcomes — exclude them from the
    # performance sample so they don't pollute expectancy / win rate / graduation.
    closed_pnls = [
        float(p["pnl_aud"]) for p in closed_pos
        if p.get("pnl_aud") is not None and p.get("status") != "closed_manual"
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

    # Portfolio-level "vs market" read for the open book (mark-to-market).
    bench = _spy_benchmark(open_pos, _fetch_fx_rate())
    if bench:
        print(bench)
