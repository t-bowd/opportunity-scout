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


def open_position(ticker: str, qty: int, opportunity_id: str) -> dict | None:
    """Submit a market BUY, wait for the fill, then rest the −12% hard stop.

    Returns {fill_price_usd, filled_qty, broker_order_id, broker_exit_order_id}
    or None if the broker is disabled / the order didn't fill / anything errored.
    """
    cli = alpaca.client()
    if cli is None:
        return None
    try:
        buy = cli.submit_market_buy(ticker, qty, client_order_id=f"os-buy-{opportunity_id}")
        filled = _await_fill(cli, buy["id"])
        if filled is None:
            print(f"[broker] {ticker} buy did not fill this run — skipping insert")
            return None
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
        print(f"[broker] open_position {ticker} failed (soft): {e}")
        return None


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
