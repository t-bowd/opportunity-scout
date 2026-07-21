"""
Sleeve exits — the heart of the strategy (SPEC §5). Precedence, first match wins:

    pre-catalyst  ->  trailing  ->  disaster  ->  time

Pure decision logic (`evaluate`, `trail_pct_for`, date helpers) is separated from
the DB wrapper (`run_exits`) so it can be smoke-tested without Supabase.

Rules deliberately INVERT the main book: exit BEFORE a binary readout (never hold
through it), a wide +30% arm, a ratcheting trail that only gently tightens, and a
-35% "thesis broken" stop rather than a -12% risk stop.
"""

from datetime import date, datetime, timedelta

from smallcap import store
from smallcap.momentum import momentum
from smallcap.pricing import current_price_native, to_aud

# --- Parameters (SPEC §5, DECIDED 2026-07-16) ---
PRE_CATALYST_EXIT_DAYS = 2       # exit N business days before a dated readout
TRAIL_ARM_PCT = 30.0             # arm the trail once up +30%
DISASTER_STOP_PCT = -35.0        # "thesis broken", not risk management
TIME_LIMIT_DAYS = 45             # non-dated (asx_ann) positions only

# Ratcheting trail: (peak_gain_pct_threshold, trail_pct), highest first, first match wins.
TRAIL_TIERS: list[tuple[float, float]] = [
    (300.0, 15.0),
    (100.0, 17.0),
    (0.0, 20.0),
]


def trail_pct_for(peak_gain_pct: float) -> float:
    """Trail width for a given peak gain — tightens gently as the winner grows."""
    for threshold, pct in TRAIL_TIERS:
        if peak_gain_pct >= threshold:
            return pct
    return TRAIL_TIERS[-1][1]


def parse_catalyst_date(s: str | None) -> date | None:
    """
    CT.gov dates come as 'YYYY-MM-DD', 'YYYY-MM', or 'YYYY'. Partial dates resolve
    to the FIRST of the period — conservative for the pre-catalyst exit (we'd
    rather bail early than risk holding into an ambiguous readout window).
    """
    if not s:
        return None
    for fmt, length in (("%Y-%m-%d", 10), ("%Y-%m", 7), ("%Y", 4)):
        if len(s) == length:
            try:
                return datetime.strptime(s, fmt).date()
            except ValueError:
                return None
    return None


def business_days_until(target: date, today: date) -> int:
    """Weekdays in (today, target]. 0 if target is today or already past."""
    if target <= today:
        return 0
    n, d = 0, today
    while d < target:
        d += timedelta(days=1)
        if d.weekday() < 5:  # Mon-Fri
            n += 1
    return n


def evaluate(pos: dict, price_aud: float, today: date) -> dict:
    """
    Pure exit decision for one position at a given AUD price. Returns:
      {action: 'hold'|'exit', reason, pnl_pct, new_peak_aud, trail_active}
    Does not touch the DB. `pos` needs: entry_price_aud, entry_date,
    peak_price_aud, trailing_stop_active, catalyst_date (may be None/'').
    """
    entry = float(pos["entry_price_aud"])
    pnl_pct = (price_aud - entry) / entry * 100 if entry else 0.0

    stored_peak = float(pos.get("peak_price_aud") or entry)
    new_peak = max(stored_peak, price_aud)
    peak_gain_pct = (new_peak - entry) / entry * 100 if entry else 0.0
    was_active = bool(pos.get("trailing_stop_active", False))
    trail_active = was_active or peak_gain_pct >= TRAIL_ARM_PCT

    result = {"action": "hold", "reason": "", "pnl_pct": round(pnl_pct, 1),
              "new_peak_aud": round(new_peak, 4), "trail_active": trail_active}

    # Only biotech has a FUTURE-dated binary readout to exit ahead of. asx_ann /
    # defense catalysts have already fired (their stored date is in the PAST), so
    # they are treated as non-dated: no pre-catalyst exit, and the time limit
    # applies instead. Keying off vertical (not "is a date present") is essential —
    # otherwise every asx_ann name closes the day after entry (its past
    # announcement date reads as "0 business days to the readout").
    is_dated = pos.get("vertical") == "biotech"
    cat = parse_catalyst_date(pos.get("catalyst_date"))

    # 1. Pre-catalyst — the edge. Exit before the biotech readout, any P&L.
    if is_dated and cat is not None and business_days_until(cat, today) <= PRE_CATALYST_EXIT_DAYS:
        result.update(action="exit", reason="pre_catalyst")
        return result

    # 2. Trailing stop — ratcheting, latched.
    if trail_active:
        trail_pct = trail_pct_for(peak_gain_pct)
        stop_price = new_peak * (1 - trail_pct / 100)
        if price_aud <= stop_price:
            result.update(action="exit", reason=f"trailing_stop_{trail_pct:.0f}pct")
            return result

    # 3. Disaster stop — thesis broken.
    if pnl_pct <= DISASTER_STOP_PCT:
        result.update(action="exit", reason="disaster_stop")
        return result

    # 4. Time limit — for non-dated (asx_ann / defense) positions whose catalyst
    # has already fired and which never ran.
    if not is_dated:
        held = (today - _parse_date(pos["entry_date"])).days
        if held >= TIME_LIMIT_DAYS:
            result.update(action="exit", reason="time_limit")
            return result

    return result


def _parse_date(s) -> date:
    if isinstance(s, date):
        return s
    return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()


def _climbing_note(ticker: str) -> str:
    """
    OBSERVATION ONLY — deliberately never influences an exit decision.

    The entry thesis is "catalyst on a name that is ALREADY climbing". When that
    momentum breaks the thesis has arguably gone, but price alone may not have
    tripped anything: JSPR (2026-07-20) peaked +25%, fell off the screen's
    climbing list, yet sat far from the -35% disaster stop and months from its
    readout. Logging the verdict next to each HOLD builds the record needed to
    judge, at the 4-6 week review, whether a momentum-invalidation exit earns a
    place — rather than narrowing the trail, which would clip the power-law
    winners the sleeve exists to catch.

    Fails soft to "" so a data hiccup can never break the exit run.
    """
    try:
        m = momentum(ticker)
    except Exception:  # noqa: BLE001 — instrumentation must not break exits
        return ""
    if not m:
        return " | climbing n/a"
    return (" | climbing" if m["climbing"]
            else f" | NOT climbing (20d {m['ret_20d']:+.0f}%)")


def run_exits() -> None:
    """Evaluate every open sleeve position and persist holds/exits."""
    today = date.today()
    positions = store.get_open_positions()
    if not positions:
        print("[smallcap/exit] no open positions")
        return

    fx_note = ""
    for pos in positions:
        tkr = pos["ticker"]
        price_native = current_price_native(tkr)
        if price_native is None:
            print(f"[smallcap/exit] {tkr} price unavailable — holding")
            continue
        price_aud = to_aud(price_native, pos["exchange"], float(pos.get("fx_rate") or 0.65))
        d = evaluate(pos, price_aud, today)
        held = (today - _parse_date(pos["entry_date"])).days

        if d["action"] == "exit":
            qty = pos["quantity"]
            pnl_aud = round((price_aud - float(pos["entry_price_aud"])) * qty, 2)
            store.close_position(pos["id"], {
                "status": "closed", "exit_date": today.isoformat(),
                "exit_price_aud": price_aud, "exit_reason": d["reason"],
                "pnl_aud": pnl_aud, "pnl_pct": d["pnl_pct"],
            })
            print(f"[smallcap/exit] CLOSED {tkr} ({d['reason']}) — "
                  f"{d['pnl_pct']:+.1f}% / ${pnl_aud:+.2f} AUD after {held}d")
        else:
            # Persist peak/trail ratchet if it advanced.
            if (d["new_peak_aud"] > float(pos.get("peak_price_aud") or 0)
                    or d["trail_active"] != bool(pos.get("trailing_stop_active"))):
                store.update_position_peak(pos["id"], d["new_peak_aud"], d["trail_active"])
            arm = " | trail armed" if d["trail_active"] else ""
            cat = pos.get("catalyst_date") or "no date"
            print(f"[smallcap/exit] HOLD {tkr} — {held}d, {d['pnl_pct']:+.1f}% "
                  f"(catalyst {cat}){arm}{_climbing_note(tkr)}")
    print(fx_note, end="")
