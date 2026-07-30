"""
Higher-level broker operations the daily run calls for US positions.

Each is a no-op returning None when the broker is disabled, so the callers in
entry.py / exit.py can guard on the return without special-casing config.

Order lifecycle (see SPEC.md §5):
  entry fill  -> submit resting hard `stop` at entry - HARD_STOP_PCT
  arm (+20%)  -> cancel that stop, submit `trailing_stop` (TRAIL_PERCENT)
  time exit   -> market sell (no native "sell after N days" order exists)
`broker_exit_order_id` on the Supabase row always points at the *current*
resting exit order, so reconciliation only has to watch that one id.
"""
from __future__ import annotations

import time

from broker import alpaca, config

# How long to wait for a market order to fill before giving up for this run. The
# daily job fires mid-US-session so paper market orders fill near-instantly; the
# rare no-fill just means we skip the insert (the DAY order expires at close).
_FILL_POLL_TRIES = 8
_FILL_POLL_SLEEP = 1.5


def _await_fill(cli: alpaca.AlpacaClient, order_id: str) -> dict | None:
    for _ in range(_FILL_POLL_TRIES):
        order = cli.get_order(order_id)
        if order.get("status") == "filled":
            return order
        if order.get("status") in ("canceled", "expired", "rejected"):
            return None
        time.sleep(_FILL_POLL_SLEEP)
    return None


DEFERRED = {"deferred": True}


def account_cash_equity_usd() -> tuple[float, float] | None:
    """(cash, equity) in USD from the live/paper broker account, or None when the
    broker is disabled / unreachable. Cash is the hard deployable budget (it
    already nets out open positions); equity backs the single-name concentration
    cap. Fails soft — a None here makes the live sizer deploy nothing this run
    rather than guess."""
    cli = alpaca.client()
    if cli is None:
        return None
    try:
        a = cli.get_account()
        return float(a["cash"]), float(a["portfolio_value"])
    except (alpaca.BrokerError, KeyError, ValueError) as e:
        print(f"[broker] account cash/equity fetch failed (soft): {e}")
        return None


def open_position(ticker: str, qty: int, opportunity_id: str) -> dict | None:
    """Submit a market BUY, wait for the fill, then rest the −12% hard stop.

    Three outcomes the caller must distinguish:
      None       -> broker DISABLED; caller falls back to the simulator.
      DEFERRED   -> broker on but the buy can't be filled right now (market
                    closed, or it didn't fill). Caller must SKIP the entry
                    entirely — NOT fall back to a sim insert, or we'd end up
                    with a queued Alpaca order and a 'sim' Supabase row for the
                    same pick. The pick is re-evaluated on the next run.
      dict       -> filled: {fill_price_usd, filled_qty, broker_order_id,
                    broker_exit_order_id}
    """
    cli = alpaca.client()
    if cli is None:
        return None

    # Never submit into a closed market. The scheduled daily fires mid-session,
    # but weekend/holiday runs and manual dispatches happen — a DAY order sent
    # then would rest and fill at the next open, behind our back.
    try:
        if not cli.get_clock().get("is_open"):
            print(f"[broker] market closed — deferring {ticker} entry to the next run")
            return DEFERRED
    except alpaca.BrokerError as e:
        print(f"[broker] clock check failed, deferring {ticker} (soft): {e}")
        return DEFERRED

    try:
        buy = cli.submit_market_buy(ticker, qty, client_order_id=f"os-buy-{opportunity_id}")
        filled = _await_fill(cli, buy["id"])
        if filled is None:
            # Cancel so it can't fill later behind our back, then defer.
            cli.cancel_order(buy["id"])
            print(f"[broker] {ticker} buy did not fill — order cancelled, entry deferred")
            return DEFERRED
        fill_px = float(filled["filled_avg_price"])
        fill_qty = int(float(filled["filled_qty"]))
        stop_px = fill_px * (1 - config.HARD_STOP_PCT / 100)
        stop = cli.submit_stop_sell(
            ticker, fill_qty, stop_px, client_order_id=f"os-stop-{opportunity_id}")
        print(f"[broker] {ticker} FILLED {fill_qty} @ ${fill_px:.2f} USD, "
              f"resting stop @ ${stop_px:.2f}")
        return {
            "fill_price_usd": fill_px,
            "filled_qty": fill_qty,
            "broker_order_id": buy["id"],
            "broker_exit_order_id": stop["id"],
        }
    except alpaca.BrokerError as e:
        # Defer rather than fall back to a sim insert: a buy may have landed
        # before the error, and a sim row would silently diverge from the broker.
        print(f"[broker] open_position {ticker} failed — deferring entry (soft): {e}")
        return DEFERRED


def arm_trailing(position: dict) -> str | None:
    """Cancel the resting hard stop and submit a trailing_stop. Returns the new
    exit order id, or None if disabled / errored (caller keeps the old id)."""
    cli = alpaca.client()
    if cli is None:
        return None
    ticker = position["ticker"]
    qty = int(position["quantity"])
    try:
        if position.get("broker_exit_order_id"):
            cli.cancel_order(position["broker_exit_order_id"])
        trail = cli.submit_trailing_stop_sell(
            ticker, qty, config.TRAIL_PERCENT,
            client_order_id=f"os-trail-{position['id']}")
        print(f"[broker] {ticker} armed — trailing_stop {config.TRAIL_PERCENT}% resting")
        return trail["id"]
    except alpaca.BrokerError as e:
        print(f"[broker] arm_trailing {ticker} failed (soft): {e}")
        return None


def time_exit(position: dict) -> str | None:
    """Cancel the resting exit and market-sell (calendar exit). Returns the sell
    order id, or None if disabled / errored."""
    cli = alpaca.client()
    if cli is None:
        return None
    ticker = position["ticker"]
    qty = int(position["quantity"])
    try:
        if position.get("broker_exit_order_id"):
            cli.cancel_order(position["broker_exit_order_id"])
        sell = cli.submit_market_sell(
            ticker, qty, client_order_id=f"os-time-{position['id']}")
        print(f"[broker] {ticker} time exit — market sell submitted")
        return sell["id"]
    except alpaca.BrokerError as e:
        print(f"[broker] time_exit {ticker} failed (soft): {e}")
        return None
