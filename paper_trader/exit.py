"""
Checks all open paper positions for exit conditions.
Runs daily.

Exit rules:
  - Time exit: position held ≥ 28 days
  - Stop loss: position down ≥ 12% from entry (in AUD terms)
"""

import requests
from datetime import date, datetime, timezone

from db.client import (
    get_open_paper_positions,
    close_paper_position,
    update_paper_position_peak,
    auto_fill_feedback_exit,
)

TRAILING_STOP_ACTIVATE_PCT = 30.0   # activate trailing stop once up 30%
TRAILING_STOP_TRAIL_PCT    = 15.0   # exit if price falls 15% below peak


def _pnl_to_grade(pnl_pct: float) -> int:
    """Convert actual P&L % into a 1-5 grade for the feedback table."""
    if pnl_pct >= 10:  return 5   # strong win
    if pnl_pct >= 2:   return 4   # modest win
    if pnl_pct >= -3:  return 3   # flat / noise
    if pnl_pct >= -8:  return 2   # small loss
    return 1                       # stopped out / big loss

MAX_HOLD_DAYS = 28
STOP_LOSS_PCT = -12.0
SLIPPAGE_PCT = 0.5      # 0.5% worse than market on exit


def _fetch_price(ticker: str) -> float | None:
    try:
        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
            "?interval=1d&range=5d"
        )
        resp = requests.get(
            url, headers={"User-Agent": "OpportunityScout"}, timeout=10
        )
        return resp.json()["chart"]["result"][0]["meta"]["regularMarketPrice"]
    except Exception:
        return None


def _fetch_fx_rate() -> float:
    try:
        url = (
            "https://query1.finance.yahoo.com/v8/finance/chart/AUDUSD=X"
            "?interval=1d&range=5d"
        )
        resp = requests.get(
            url, headers={"User-Agent": "OpportunityScout"}, timeout=10
        )
        return resp.json()["chart"]["result"][0]["meta"]["regularMarketPrice"]
    except Exception:
        print("[paper/exit] FX rate fetch failed, using fallback 0.65")
        return 0.65


def run_exits() -> None:
    positions = get_open_paper_positions()
    if not positions:
        print("[paper/exit] no open positions")
        return

    today = date.today()
    fx_rate = _fetch_fx_rate()
    now_iso = datetime.now(timezone.utc).isoformat()

    for pos in positions:
        ticker = pos["ticker"]
        entry_date = date.fromisoformat(pos["entry_date"])
        days_held = (today - entry_date).days
        entry_price_aud = float(pos["entry_price_aud"])
        quantity = pos["quantity"]
        brokerage = float(pos.get("brokerage_aud", 0))

        current_price = _fetch_price(ticker)
        if current_price is None:
            print(f"[paper/exit] {ticker} — price fetch failed, holding")
            continue

        is_asx = ticker.endswith(".AX")

        if is_asx:
            # ASX prices already in AUD; slippage applies on exit (sell lower)
            exit_price_aud = round(current_price * (1 - SLIPPAGE_PCT / 100), 4)
            exit_price_usd = round(exit_price_aud * fx_rate, 4)
        else:
            # US price in USD; apply slippage then convert
            exit_price_usd = round(current_price * (1 - SLIPPAGE_PCT / 100), 4)
            exit_price_aud = round(exit_price_usd / fx_rate, 4)

        pnl_aud = round((exit_price_aud - entry_price_aud) * quantity - brokerage, 2)
        pnl_pct = round(
            (exit_price_aud - entry_price_aud) / entry_price_aud * 100, 2
        )

        # --- Trailing stop maintenance ---
        # Update peak price and activate trailing stop if threshold reached
        current_peak = float(pos.get("peak_price_aud") or entry_price_aud)
        trailing_active = bool(pos.get("trailing_stop_active", False))

        new_peak = max(current_peak, exit_price_aud)
        should_activate = pnl_pct >= TRAILING_STOP_ACTIVATE_PCT

        if new_peak != current_peak or should_activate != trailing_active:
            update_paper_position_peak(pos["id"], new_peak, should_activate)
            if should_activate and not trailing_active:
                print(
                    f"[paper/exit] TRAILING STOP ACTIVATED {ticker} — "
                    f"up {pnl_pct:+.1f}%, peak ${new_peak:.2f} AUD, "
                    f"stop at ${new_peak * (1 - TRAILING_STOP_TRAIL_PCT / 100):.2f} AUD"
                )

        # --- Exit evaluation (priority order) ---
        trailing_stop_price = new_peak * (1 - TRAILING_STOP_TRAIL_PCT / 100)

        if should_activate and exit_price_aud < trailing_stop_price:
            exit_reason = "trailing_stop"
            new_status = "closed_trail"
        elif pnl_pct <= STOP_LOSS_PCT:
            exit_reason = "stop_loss"
            new_status = "closed_stop"
        elif days_held >= MAX_HOLD_DAYS:
            exit_reason = "time_exit"
            new_status = "closed_time"
        else:
            trail_note = (
                f" | trailing stop active, floor ${trailing_stop_price:.2f}"
                if should_activate else ""
            )
            print(
                f"[paper/exit] HOLD {ticker} — "
                f"{days_held}d, {pnl_pct:+.1f}% (${pnl_aud:+.2f} AUD){trail_note}"
            )
            continue

        close_paper_position(pos["id"], {
            "status": new_status,
            "exit_price_usd": exit_price_usd,
            "exit_price_aud": exit_price_aud,
            "exit_date": today.isoformat(),
            "exit_reason": exit_reason,
            "pnl_aud": pnl_aud,
            "pnl_pct": pnl_pct,
            "updated_at": now_iso,
        })
        if pos.get("opportunity_id"):
            auto_fill_feedback_exit(pos["opportunity_id"], _pnl_to_grade(pnl_pct))
        print(
            f"[paper/exit] CLOSED {ticker} ({exit_reason}) — "
            f"{pnl_pct:+.1f}% / ${pnl_aud:+.2f} AUD after {days_held}d"
        )
