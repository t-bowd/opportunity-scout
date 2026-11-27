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
from datetime import date, datetime, timezone

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


def _day(epoch_seconds: int) -> date:
    return datetime.fromtimestamp(int(epoch_seconds), tz=timezone.utc).date()


def _parse_inception(hist: dict) -> dict | None:
    """Reduce a portfolio-history payload to the since-inception facts.

    Pure, so the arithmetic is testable without a broker. Returns
    {start_date, start_equity, equity, net_flows_usd, days} in USD, or None when
    the payload is too thin to say anything.

    "Inception" is the first day the account held anything: the zero-equity days
    before funding are not part of the record, and dating the book from them
    would compare the strategy against a benchmark it wasn't invested in yet.

    Flows are counted only STRICTLY AFTER that first funded day. The deposit that
    funded the account lands on day one and already *is* start_equity, so
    counting it again would net the opening balance to zero and report an
    infinite return. Later deposits are genuine additions to capital.
    """
    ts = hist.get("timestamp") or []
    eq = hist.get("equity") or []
    if len(ts) != len(eq) or len(ts) < 2:
        return None

    funded = []
    for t, e in zip(ts, eq):
        if e is None or e == "":
            continue
        try:
            value = float(e)
        except (TypeError, ValueError):
            continue
        if value > 0:
            funded.append((t, value))
    if len(funded) < 2:
        return None

    start_ts, start_equity = funded[0]
    end_ts, equity = funded[-1]

    # Alpaca's own docs call the cashflow arrays "accumulated" but ship an
    # example that isn't monotonic, so read them as per-window amounts and sum.
    # Over-counting here would understate the return, which is the safe direction
    # for a number used to decide whether real money keeps running.
    flows = 0.0
    for series in (hist.get("cashflow") or {}).values():
        for t, amount in zip(ts, series or []):
            if t <= start_ts or not amount:
                continue
            try:
                flows += float(amount)
            except (TypeError, ValueError):
                continue

    # Derived here, not at each call site: the capital base is start_equity PLUS
    # later transfers, and getting that denominator wrong is the whole trap this
    # function exists to avoid. One definition, one place to test it.
    invested = round(start_equity + flows, 2)
    return {
        "start_date": _day(start_ts).isoformat(),
        "start_equity": round(start_equity, 2),
        "equity": round(equity, 2),
        "net_flows_usd": round(flows, 2),
        "invested_usd": invested,
        "return_pct": round((equity / invested - 1) * 100, 2) if invested > 0 else None,
        "days": (_day(end_ts) - _day(start_ts)).days,
    }


def account_since_inception_usd() -> dict | None:
    """What the account has done since the money went in — see _parse_inception.

    None when the broker is disabled, unreachable, or the history is unusable.
    Fails soft: this line is instrumentation, and losing it must never cost the
    snapshot the rest of its output.
    """
    cli = alpaca.client()
    if cli is None:
        return None
    try:
        return _parse_inception(cli.get_portfolio_history())
    except (alpaca.BrokerError, KeyError, ValueError, TypeError) as e:
        print(f"[broker] portfolio history fetch failed (soft): {e}")
        return None


def open_position(ticker: str, qty: int, opportunity_id: str,
                  stop_pct: float | None = None) -> dict | None:
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
        # Per-position stop distance (volatility-scaled at entry). Falls back to the
        # flat legacy stop when the caller doesn't supply one, so nothing can open
        # without a resting line under it.
        stop_distance = stop_pct if stop_pct and stop_pct > 0 else config.HARD_STOP_PCT
        stop_px = fill_px * (1 - stop_distance / 100)
        stop = cli.submit_stop_sell(
            ticker, fill_qty, stop_px, client_order_id=f"os-stop-{opportunity_id}")
        print(f"[broker] {ticker} FILLED {fill_qty} @ ${fill_px:.2f} USD, "
              f"resting stop @ ${stop_px:.2f} (−{stop_distance:.1f}%)")
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


def move_stop_to_breakeven(position: dict) -> str | None:
    """Cancel the resting hard stop and re-rest it at the ENTRY price.

    Called once, when a position's peak gain first clears BREAKEVEN_ARM_PCT but
    before the +20% trail arms — so a name that runs up and then rolls over
    scratches at ~0% instead of giving back the whole move AND taking the −12%
    loss. Returns the new exit order id, or None if disabled/errored (caller keeps
    the old resting stop, so a failure is soft — the −12% protection stays)."""
    cli = alpaca.client()
    if cli is None:
        return None
    ticker = position["ticker"]
    qty = int(position["quantity"])
    entry_px = float(position.get("entry_price_usd") or 0)
    if entry_px <= 0:
        return None
    try:
        if position.get("broker_exit_order_id"):
            cli.cancel_order(position["broker_exit_order_id"])
        stop = cli.submit_stop_sell(
            ticker, qty, entry_px, client_order_id=f"os-be-{position['id']}")
        print(f"[broker] {ticker} stop RAISED TO BREAKEVEN @ ${entry_px:.2f}")
        return stop["id"]
    except alpaca.BrokerError as e:
        print(f"[broker] move_stop_to_breakeven {ticker} failed (soft): {e}")
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


# An Alpaca order in one of these states is finished and will never fill. Anything
# NOT here is still working, so treating the set as the terminal one (rather than
# listing the live states) fails safe: an unfamiliar status reads as "still live"
# and we leave it alone instead of cancelling something that was about to fill.
_TERMINAL_ORDER_STATUSES = frozenset({
    "filled", "canceled", "cancelled", "expired", "rejected",
    "done_for_day", "replaced", "suspended", "stopped",
})


def _exit_order_state(cli: alpaca.AlpacaClient, order_id: str) -> str:
    """Classify a position's currently recorded resting exit order.

      "pending_sell" -> a market SELL still working: the time exit is already in
                        and waiting for the open. LEAVE IT ALONE. A sell submitted
                        after the US close is queued for an open up to fourteen
                        hours away, and the daily job fires 3-5 times in that gap;
                        cancelling it there is what wedged BWMX for four days.
      "live_other"   -> a working stop or trailing stop. Cancel before selling.
      "gone"         -> filled, cancelled, expired, rejected, or unreadable. Do NOT
                        attempt to cancel it: Alpaca answers 422 "order is not
                        cancelable" for an already-terminal order, and
                        cancel_order() only swallows 404, so the raise would abort
                        the submit and keep the position wedged — which is exactly
                        the state BWMX is in, since its sell was cancelled by a
                        later run on the same day.

    One read, three outcomes, because the cancel decision and the resubmit decision
    both depend on the same fact and must not disagree.
    """
    try:
        order = cli.get_order(order_id)
    except alpaca.BrokerError:
        return "gone"
    if order.get("status") in _TERMINAL_ORDER_STATUSES:
        return "gone"
    if order.get("type") == "market" and order.get("side") == "sell":
        return "pending_sell"
    return "live_other"


def time_exit(position: dict) -> str | None:
    """Cancel the resting exit and market-sell (calendar exit). Returns the order
    id of the working sell, or None if disabled / errored.

    Unlike arm_trailing and move_stop_to_breakeven — which fire exactly once, on the
    run a threshold is crossed — this is RETRIED every run for as long as the position
    sits past its time limit, which makes the retry itself the hazard.

    Two bugs have come from that, and both are fixed here:

      1. A deterministic client_order_id meant the first attempt consumed it and
         Alpaca rejected every later one with 422 "client_order_id must be unique"
         (BWFG, from 2026-10-11). Date-stamping gave ONE attempt per day — but the
         workflow fires 3-5 times a day, so every run after the first still 422'd.
         The id is now unique per ATTEMPT.
      2. Worse, the retry cancelled the pending sell before resubmitting. Submitted
         after the close, that sell is queued for an open that is hours away, so the
         pre-open retry destroyed it and then could not replace it. BWMX sat wedged
         for four days (2026-11-23 to 11-27): one clean submit at ~00:30 UTC, killed
         at ~01:30 UTC, 422 for the rest of the day, repeat. It also left the
         position with NO resting order at all, since the stop had been cancelled on
         the first attempt. A pending sell is now left alone.
      3. And the recovery path had the same shape of fault: cancelling an ALREADY
         cancelled order returns 422 "not cancelable", which cancel_order() re-raises
         because it only swallows 404 — aborting the submit. So a position that had
         once been wedged by (2) stayed wedged even with (1) and (2) fixed. We now
         read the order's state first and only cancel what is actually live.

    Cancel still precedes any NEW submit, so there is no double-sell risk. Note that
    cancel_order() is deliberately NOT made tolerant of 422 instead: arm_trailing and
    move_stop_to_breakeven cancel a LIVE stop before adding their own sell, and a
    silently-swallowed failure there would leave two sell orders against the same
    shares.
    """
    cli = alpaca.client()
    if cli is None:
        return None
    ticker = position["ticker"]
    qty = int(position["quantity"])
    existing = position.get("broker_exit_order_id")
    try:
        state = _exit_order_state(cli, existing) if existing else "gone"
        if state == "pending_sell":
            # Already working — say so and return the SAME id, so the caller treats
            # this as a success rather than printing the stuck-position warning.
            print(f"[broker] {ticker} time exit already resting at the broker "
                  f"— waiting for the open, not resubmitting")
            return existing
        if state == "live_other":
            cli.cancel_order(existing)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        sell = cli.submit_market_sell(
            ticker, qty, client_order_id=f"os-time-{position['id']}-{stamp}")
        print(f"[broker] {ticker} time exit — market sell submitted")
        return sell["id"]
    except alpaca.BrokerError as e:
        print(f"[broker] time_exit {ticker} failed (soft): {e}")
        return None
