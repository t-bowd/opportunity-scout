"""
Checks all open paper positions for exit conditions.
Runs daily.

Exit rules (priority order):
  - Trailing stop: once the position has gained TRAILING_STOP_ACTIVATE_PCT the
    stop ARMS (latched — it stays armed), then trails TRAILING_STOP_TRAIL_PCT
    below the running peak; exit when price touches that floor.
  - Stop loss: position down ≥ 12% from entry (in AUD terms)
  - Time exit: held past the per-pattern horizon (insider/13F edges play out over
    months, so they get longer than fast news/thematic plays) — but a position
    whose trailing stop is already armed is exempt, so live winners aren't cut
    by the calendar.

Live-trading note: the trail here is evaluated once per daily run (a price poll).
When wiring a real broker, do NOT keep this as a poll — submit a broker-native
trailing-stop order (trail_percent = TRAILING_STOP_TRAIL_PCT) once the position
crosses +TRAILING_STOP_ACTIVATE_PCT, so the broker tracks the intraday peak and
fills at the floor intraday. The parameters carry over unchanged; only the
execution moves from a daily poll to a resting exchange order.
"""

import requests
from datetime import date, datetime, timezone

from db.client import (
    get_open_paper_positions,
    close_paper_position,
    update_paper_position_peak,
    update_paper_position_broker_exit,
    auto_fill_feedback_exit,
)
from paper_trader.notify import notify_closed
from broker import execution as broker_execution, config as broker_config

TRAILING_STOP_ACTIVATE_PCT = 20.0   # arm the trailing stop once the position has gained 20%
TRAILING_STOP_TRAIL_PCT    = 8.0    # then exit if price falls 8% below the running peak. Tightened
                                    # from 10% on 2026-07-24: XMTR/GLBE both trail-closed the same
                                    # day having peaked ~+27%/+21% but only locked +6.5%/+7.0%, so
                                    # the wide trail gave back most of the gain. 8% captures ~+2.5pts
                                    # more from the peak on a clean trigger, accepting slightly earlier
                                    # exits on normal pullbacks. This is the medium-term insider/
                                    # smart-money book, NOT the moonshot sleeve — the sleeve keeps its
                                    # deliberately wide 20/17/15% tiers so it doesn't clip a 10x.


def _pnl_to_grade(pnl_pct: float) -> int:
    """Convert actual P&L % into a 1-5 grade for the feedback table."""
    if pnl_pct >= 10:  return 5   # strong win
    if pnl_pct >= 2:   return 4   # modest win
    if pnl_pct >= -3:  return 3   # flat / noise
    if pnl_pct >= -8:  return 2   # small loss
    return 1                       # stopped out / big loss

# Per-pattern holding horizon (days). Insider buys, 13F new positions and activist
# stakes are medium-term edges that accrue over months — give them room. News and
# thematic plays are shorter-lived. A position whose trailing stop is active is
# exempt from the time exit entirely (let winners run).
MAX_HOLD_DAYS_BY_PATTERN = {
    "insider_buy":   60,
    "smart_money":   60,
    "activist":      60,
    "spin_off":      60,
    "s1_filed":      45,
    "pre_ipo_proxy": 45,
    "thematic_etf":  30,
    "etf_launch":    30,
}
DEFAULT_MAX_HOLD_DAYS = 45
STOP_LOSS_PCT = -12.0
SLIPPAGE_PCT = 0.5      # 0.5% worse than market on exit

# Time-exit reprieve for slow, still-developing winners. In a ~63%-win, −12%-capped
# book the profit lives in the right tail, and the forward-return analysis shows
# winners peak in MONTH 2 — but the calendar time-exit cuts a GREEN name at the
# limit if it hasn't armed (+20%) yet, clipping exactly those slow month-2 winners
# before they can arm. So a name that has already shown a real move (peak ≥ MIN_PEAK)
# and is still holding near that peak (within BAND of it = climbing, not faded) earns
# a BOUNDED extension past the limit — enough time to arm or fade on its own. Armed
# names are already exempt upstream; a name that round-tripped well off its peak gets
# no reprieve and time-exits as before.
TIME_EXIT_REPRIEVE_MIN_PEAK_PCT = 10.0  # must have reached +10% peak (halfway to the arm)
TIME_EXIT_REPRIEVE_BAND_PCT     = 10.0  # ...and still be within 10% of that peak
TIME_EXIT_REPRIEVE_DAYS         = 30    # cap the extension so nothing is held forever


def _time_exit_reprieved(days_held: int, max_hold: int, peak_gain_pct: float,
                         exit_price: float, peak_price: float) -> bool:
    """Pure: should a not-yet-armed name past its time limit get extra leash?
    True only while it showed a real move, is still near its peak, and hasn't
    used up the bounded extension. Smoke-tested — this gates a real-money exit."""
    if peak_gain_pct < TIME_EXIT_REPRIEVE_MIN_PEAK_PCT:
        return False
    if days_held >= max_hold + TIME_EXIT_REPRIEVE_DAYS:
        return False
    return peak_price > 0 and exit_price >= peak_price * (1 - TIME_EXIT_REPRIEVE_BAND_PCT / 100)


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
        # Track the running peak and arm the trailing stop once the position has
        # gained TRAILING_STOP_ACTIVATE_PCT. Activation LATCHES: once armed it stays
        # armed (we read the stored flag, not today's P&L), so a winner that ticks
        # back under the activation line keeps its trailing protection instead of
        # silently disarming. The peak only ever ratchets up. This mirrors a
        # broker-native trailing-stop order — see the live-trading note up top.
        current_peak = float(pos.get("peak_price_aud") or entry_price_aud)
        was_active = bool(pos.get("trailing_stop_active", False))

        new_peak = max(current_peak, exit_price_aud)
        peak_gain_pct = (new_peak - entry_price_aud) / entry_price_aud * 100
        trailing_active = was_active or peak_gain_pct >= TRAILING_STOP_ACTIVATE_PCT

        if new_peak != current_peak or trailing_active != was_active:
            update_paper_position_peak(pos["id"], new_peak, trailing_active)
            if trailing_active and not was_active:
                print(
                    f"[paper/exit] TRAILING STOP ARMED {ticker} — "
                    f"peak +{peak_gain_pct:.1f}%, peak ${new_peak:.2f} AUD, "
                    f"stop at ${new_peak * (1 - TRAILING_STOP_TRAIL_PCT / 100):.2f} AUD"
                )

        # --- Broker-managed positions: Alpaca owns the stops ---
        # For US positions executing through Alpaca, the trailing stop and the
        # −12% hard stop are resting exchange orders that fill INTRADAY; those
        # closes come back via broker/reconcile, not this poll. Here we only
        # (a) swap the resting hard stop for a trailing stop when the position
        # arms, and (b) submit a market sell for a calendar time-exit (no native
        # order for that). Everything else just reports. If the broker is somehow
        # disabled while a position is still tagged 'alpaca', we fall through to
        # the simulator logic below so the position is never left unmanaged.
        if pos.get("broker") == "alpaca" and broker_config.enabled():
            max_hold = MAX_HOLD_DAYS_BY_PATTERN.get(pos.get("pattern", ""), DEFAULT_MAX_HOLD_DAYS)
            reprieved = (days_held >= max_hold and not trailing_active
                         and _time_exit_reprieved(days_held, max_hold, peak_gain_pct,
                                                  exit_price_aud, new_peak))
            if trailing_active and not was_active:
                new_exit_id = broker_execution.arm_trailing(pos)
                if new_exit_id:
                    update_paper_position_broker_exit(pos["id"], new_exit_id)
            elif days_held >= max_hold and not trailing_active and not reprieved:
                sell_id = broker_execution.time_exit(pos)
                if sell_id:
                    update_paper_position_broker_exit(pos["id"], sell_id)
                    print(f"[paper/exit] {ticker} — time exit sent to broker "
                          f"(fills intraday, records on next reconcile)")
                continue
            armed_note = (
                f"trailing stop live @ broker (peak +{peak_gain_pct:.1f}%)"
                if trailing_active
                else f"green+climbing past {max_hold}d — time-exit reprieved (peak +{peak_gain_pct:.1f}%)"
                if reprieved
                else f"{max_hold - days_held}d to {max_hold}d time limit"
            )
            print(f"[paper/exit] HOLD {ticker} (broker) — "
                  f"{days_held}d, {pnl_pct:+.1f}% (${pnl_aud:+.2f} AUD) | {armed_note}")
            continue

        # --- Exit evaluation (priority order) ---
        trailing_stop_price = new_peak * (1 - TRAILING_STOP_TRAIL_PCT / 100)
        max_hold = MAX_HOLD_DAYS_BY_PATTERN.get(pos.get("pattern", ""), DEFAULT_MAX_HOLD_DAYS)
        # Time exit applies only if the trailing stop ISN'T armed — let live
        # winners run on the trailing stop instead of cutting them by the calendar.
        # A green name still climbing toward the arm also gets a bounded reprieve
        # so slow month-2 winners aren't cut early (see _time_exit_reprieved).
        reprieved = (days_held >= max_hold and not trailing_active
                     and _time_exit_reprieved(days_held, max_hold, peak_gain_pct,
                                              exit_price_aud, new_peak))
        time_exit_due = days_held >= max_hold and not trailing_active and not reprieved

        if trailing_active and exit_price_aud < trailing_stop_price:
            exit_reason = "trailing_stop"
            new_status = "closed_trail"
        elif pnl_pct <= STOP_LOSS_PCT:
            exit_reason = "stop_loss"
            new_status = "closed_stop"
        elif time_exit_due:
            exit_reason = "time_exit"
            new_status = "closed_time"
        else:
            if trailing_active:
                # past the time limit only reachable here when trailing stop is armed
                extra = " (past time limit — trailing stop running)" if days_held >= max_hold else ""
                trail_note = f" | trailing stop armed, floor ${trailing_stop_price:.2f}{extra}"
            elif reprieved:
                trail_note = (f" | green+climbing past {max_hold}d — time-exit reprieved "
                              f"(peak +{peak_gain_pct:.1f}%, up to {max_hold + TIME_EXIT_REPRIEVE_DAYS}d)")
            else:
                trail_note = f" | {max_hold - days_held}d to {max_hold}d time limit"
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
        notify_closed(ticker, exit_reason, pnl_aud, pnl_pct, days_held)
