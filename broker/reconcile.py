"""
Reconcile Alpaca fills back into Supabase.

Runs first in the daily flow (before the poll-based exits). For every open
Supabase position that Alpaca is managing, it checks whether that position's
current resting exit order has filled — if so it closes the Supabase row with
the *real* fill price, time, and reason. This is where a stop that fired
intraday, between daily runs, gets recorded in the book of record.

Supabase stays the source of truth (SPEC.md §2); Alpaca just tells us what
already happened.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from broker import alpaca
from db.client import (
    get_open_paper_positions,
    close_paper_position,
    auto_fill_feedback_exit,
)
from paper_trader.exit import _fetch_fx_rate, _pnl_to_grade
from paper_trader.notify import notify_closed


def _exit_reason_for_order(order: dict) -> tuple[str, str]:
    """Map a filled Alpaca SELL onto (exit_reason, paper_positions.status)."""
    otype = order.get("type")
    if otype == "trailing_stop":
        return "trailing_stop", "closed_trail"
    if otype == "stop":
        return "stop_loss", "closed_stop"
    # the only market SELL we ever submit is the calendar exit
    return "time_exit", "closed_time"


def _fill_date(order: dict) -> date:
    ts = order.get("filled_at")
    if ts:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).date()
    return datetime.now(timezone.utc).date()


def run() -> None:
    cli = alpaca.client()
    if cli is None:
        return
    alpaca_positions = [
        p for p in get_open_paper_positions() if p.get("broker") == "alpaca"
    ]
    if not alpaca_positions:
        return

    print(f"[broker/reconcile] checking {len(alpaca_positions)} Alpaca-managed position(s)")
    fx_rate = _fetch_fx_rate()
    closed = 0
    for pos in alpaca_positions:
        exit_order_id = pos.get("broker_exit_order_id")
        if not exit_order_id:
            continue
        try:
            order = cli.get_order(exit_order_id)
        except alpaca.BrokerError as e:
            print(f"[broker/reconcile] {pos['ticker']} order lookup failed (soft): {e}")
            continue
        if order.get("status") != "filled":
            continue

        exit_price_usd = float(order["filled_avg_price"])
        exit_price_aud = round(exit_price_usd / fx_rate, 4)
        entry_price_aud = float(pos["entry_price_aud"])
        qty = int(pos["quantity"])
        brokerage = float(pos.get("brokerage_aud") or 0)
        pnl_aud = round((exit_price_aud - entry_price_aud) * qty - brokerage, 2)
        pnl_pct = round((exit_price_aud / entry_price_aud - 1) * 100, 2)
        exit_reason, new_status = _exit_reason_for_order(order)
        exit_date = _fill_date(order)
        days_held = (exit_date - date.fromisoformat(pos["entry_date"])).days

        close_paper_position(pos["id"], {
            "status": new_status,
            "exit_price_usd": exit_price_usd,
            "exit_price_aud": exit_price_aud,
            "exit_date": exit_date.isoformat(),
            "exit_reason": exit_reason,
            "pnl_aud": pnl_aud,
            "pnl_pct": pnl_pct,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        if pos.get("opportunity_id"):
            auto_fill_feedback_exit(pos["opportunity_id"], _pnl_to_grade(pnl_pct))
        print(f"[broker/reconcile] CLOSED {pos['ticker']} ({exit_reason}) — "
              f"{pnl_pct:+.1f}% / ${pnl_aud:+.2f} AUD after {days_held}d [broker fill]")
        notify_closed(pos["ticker"], exit_reason, pnl_aud, pnl_pct, days_held)
        closed += 1

    print(f"[broker/reconcile] done — {closed} closed from broker fills")
