"""
Sleeve entries (SPEC §3-4, §6). Runs the screener, then opens EQUAL-WEIGHT paper
positions on climbing catalyst names — the opposite of the main book's
conviction-scaled sizing, because in a power-law regime you can't predict which
name moons.

Gates: climbing + supported vertical + cap in [$10M,$500M] + catalyst freshness
(biotech future-dated, asx_ann within 10d) + not already held + per-vertical cap
+ free slot under the HARD pool.

Pure gate logic (`passes_gates`) is separated from the DB wrapper (`run_entries`).
"""

from datetime import date

from smallcap import store
from smallcap.screen import run_screen
from smallcap.exit import parse_catalyst_date
from smallcap.pricing import fetch_fx_rate, to_aud

# --- Parameters (SPEC, DECIDED 2026-07-16) ---
# Widened 2026-08-26 (paper sleeve, no real money): the book had been full at
# 10/10 for weeks, so it was NOT capturing the ongoing flow of climbing screen
# names — starving the research dataset the 4-6wk review needs. Doubled the SLOT
# COUNT (and pool + per-vertical cap to match), NOT the position size, so we get
# ~2x the picks/data at the same $100 equal weight. Positions stay tiny by design.
SMALLCAP_POOL_AUD = 2000.0        # HARD cap (unlike the main book's soft pool)
SMALLCAP_POSITION_AUD = 100.0     # equal weight — no conviction scaling
SMALLCAP_MAX_POSITIONS = 20
MAX_PER_VERTICAL = 10             # correlation cap; raised with the slot count so
                                  # a vertical can actually fill the new slots
MIN_CAP = 10e6
MAX_CAP = 500e6
ASX_ANN_MAX_AGE_DAYS = 10
SUPPORTED_VERTICALS = {"biotech", "asx_ann", "defense"}

US_BROKERAGE_AUD = 1.0
ASX_BROKERAGE_AUD = 0.0
SLIPPAGE_PCT = 2.0                # micro-caps have wide spreads; don't flatter fills


def passes_gates(cand: dict, held_tickers: set[str], vertical_counts: dict[str, int],
                 today: date) -> tuple[bool, str]:
    """Pure entry gate. Returns (ok, reason-if-skipped)."""
    if not cand.get("climbing"):
        return False, "not_climbing"
    vert = cand.get("vertical", "")
    if vert not in SUPPORTED_VERTICALS:
        return False, f"unsupported_vertical:{vert}"
    cap = cand.get("market_cap") or 0
    if not (MIN_CAP <= cap <= MAX_CAP):
        return False, f"cap_out_of_band:{cap/1e6:.0f}M"

    cat = parse_catalyst_date(cand.get("catalyst_date"))
    if vert == "biotech":
        if cat is None or cat < today:
            return False, "biotech_not_future_dated"
    elif vert == "asx_ann":
        if cat is None or (today - cat).days > ASX_ANN_MAX_AGE_DAYS or cat > today:
            return False, "asx_ann_stale"

    if cand["ticker"] in held_tickers:
        return False, "already_held"
    if vertical_counts.get(vert, 0) >= MAX_PER_VERTICAL:
        return False, f"vertical_cap_full:{vert}"
    return True, ""


def run_entries() -> None:
    today = date.today()
    open_positions = store.get_open_positions()
    held = {p["ticker"] for p in open_positions}
    vcounts: dict[str, int] = {}
    for p in open_positions:
        vcounts[p["vertical"]] = vcounts.get(p["vertical"], 0) + 1
    deployed = sum(float(p["entry_price_aud"]) * p["quantity"]
                   + float(p.get("brokerage_aud") or 0) for p in open_positions)
    slots_left = SMALLCAP_MAX_POSITIONS - len(open_positions)

    if slots_left <= 0:
        print(f"[smallcap/entry] book full ({len(open_positions)}/{SMALLCAP_MAX_POSITIONS})")
        return
    if SMALLCAP_POOL_AUD - deployed < SMALLCAP_POSITION_AUD:
        print(f"[smallcap/entry] pool exhausted (${deployed:.0f}/${SMALLCAP_POOL_AUD:.0f})")
        return

    candidates = run_screen(max_cap=MAX_CAP, min_cap=MIN_CAP, climbing_only=True)
    fx_rate = fetch_fx_rate()
    entered = 0

    for cand in candidates:
        if slots_left <= 0 or SMALLCAP_POOL_AUD - deployed < SMALLCAP_POSITION_AUD:
            break
        ok, reason = passes_gates(cand, held, vcounts, today)
        if not ok:
            # Only log the "interesting" skips, not every out-of-band name.
            if reason.startswith(("vertical_cap", "already_held")):
                print(f"[smallcap/entry] SKIP {cand['ticker']} ({cand['vertical']}) — {reason}")
                store.insert_skipped({"ticker": cand["ticker"], "vertical": cand["vertical"],
                                      "reason": reason, "skip_date": today.isoformat()})
            continue

        is_asx = cand["exchange"] == "ASX"
        price_native = cand.get("momentum", {}).get("price") if cand.get("momentum") else None
        if not price_native:
            continue
        entry_native = price_native * (1 + SLIPPAGE_PCT / 100)
        entry_aud = to_aud(entry_native, cand["exchange"], fx_rate)
        brokerage = ASX_BROKERAGE_AUD if is_asx else US_BROKERAGE_AUD
        qty = int(SMALLCAP_POSITION_AUD / entry_aud)
        if qty < 1:
            continue
        cost = entry_aud * qty + brokerage
        if deployed + cost > SMALLCAP_POOL_AUD:
            continue

        # Normalise the catalyst date to a full ISO date — the feed gives partial
        # 'YYYY-MM' values that a Postgres `date` column rejects. parse_catalyst_date
        # resolves partials to the 1st of the period (conservative for the
        # pre-catalyst exit — bail early rather than hold into an ambiguous window).
        cat_parsed = parse_catalyst_date(cand.get("catalyst_date"))
        store.insert_position({
            "ticker": cand["ticker"], "name": cand["name"], "exchange": cand["exchange"],
            "vertical": cand["vertical"], "catalyst": cand.get("catalyst", ""),
            "catalyst_date": cat_parsed.isoformat() if cat_parsed else None,
            "market_cap": cand["market_cap"],
            "entry_date": today.isoformat(),
            "entry_price_native": round(entry_native, 4), "entry_price_aud": entry_aud,
            "quantity": qty, "fx_rate": fx_rate, "brokerage_aud": brokerage,
            "peak_price_aud": entry_aud, "trailing_stop_active": False, "status": "open",
        })
        held.add(cand["ticker"])
        vcounts[cand["vertical"]] = vcounts.get(cand["vertical"], 0) + 1
        deployed += cost
        slots_left -= 1
        entered += 1
        print(f"[smallcap/entry] ENTER {cand['ticker']} ({cand['exchange']}, {cand['vertical']}) "
              f"@ ${entry_aud:.4f} AUD × {qty} = ${entry_aud*qty:.2f} (cap ${cand['market_cap']/1e6:.0f}M, "
              f"catalyst {cand.get('catalyst_date') or 'n/a'})")

    print(f"[smallcap/entry] done — {entered} entered, "
          f"{len(open_positions)+entered}/{SMALLCAP_MAX_POSITIONS} open, "
          f"${deployed:.0f}/${SMALLCAP_POOL_AUD:.0f} deployed")
