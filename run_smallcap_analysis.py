"""
Sleeve ENTRY-QUALITY analysis — read-only, decides nothing.

Every result the sleeve has produced points at the same suspect. The exit machinery
demonstrably works (CRDL +58.6% and ACW +38.8% both banked by the pre-catalyst rule;
disaster stops and time limits all fire), yet realized P&L is deeply negative. So the
leak is upstream: WHICH names get bought.

The lead hypothesis is ENTRY EXTENSION. The entry gate requires a name to be
"climbing", but climbing cannot distinguish a move that is just starting from one
already blown off. Anecdotally the split is stark — CRDL and ACW were bought early in
their moves and won; JSPR (bought after a +104% 20-day run) and GES.AX (bought after
+180%) were bought extended and lost ~30%. Four trades is a story, not evidence.

This script turns it into evidence. `smallcap_momentum` has recorded each position's
20-session return every day since 2026-08-10, INCLUDING its entry day, so for every
closed position we can ask what its momentum looked like at the moment we bought it
and compare that against what it actually returned.

Deliberately reports and decides nothing — same contract as run_analysis.py. Read it,
then choose a threshold from the data instead of guessing one.

Usage:  python run_smallcap_analysis.py
"""

import statistics
import sys
from datetime import date

from dotenv import load_dotenv

load_dotenv(".env.local")

from smallcap import store
from smallcap.exit import business_days_until, PRE_CATALYST_EXIT_DAYS
from smallcap.entry import SLIPPAGE_PCT

MIN_N = 3          # don't report a bucket thinner than this
ENTRY_OBS_MAX_LAG_DAYS = 3   # an "entry-day" reading must actually be near entry

# Buckets chosen to straddle the anecdotes: CRDL/ACW entered under ~+20%, JSPR at
# +104%, GES at +180%. If extension matters, the damage should show up to the right.
EXT_BUCKETS = [(-999.0, 0.0, "<=0%"), (0.0, 20.0, "0-20%"), (20.0, 50.0, "20-50%"),
               (50.0, 100.0, "50-100%"), (100.0, 9999.0, ">100%")]
CAP_BUCKETS = [(0, 25e6, "<$25M"), (25e6, 100e6, "$25-100M"),
               (100e6, 250e6, "$100-250M"), (250e6, 1e12, ">$250M")]
PROX_BUCKETS = [(0, 10, "<=10bd"), (10, 30, "10-30bd"),
                (30, 60, "30-60bd"), (60, 99999, ">60bd")]


def _fmt_pct(v):
    return "   n/a " if v is None else f"{v:+6.1f}%"


def _pearson(xs, ys):
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    if len(pairs) < 3:
        return None
    xs2, ys2 = [p[0] for p in pairs], [p[1] for p in pairs]
    mx, my = statistics.mean(xs2), statistics.mean(ys2)
    num = sum((x - mx) * (y - my) for x, y in pairs)
    dx = sum((x - mx) ** 2 for x in xs2) ** 0.5
    dy = sum((y - my) ** 2 for y in ys2) ** 0.5
    return None if dx == 0 or dy == 0 else num / (dx * dy)


def _agg(subset):
    """(n, mean, median, win%) over realized pnl_pct."""
    rs = [d["pnl_pct"] for d in subset]
    win = sum(1 for r in rs if r > 0) / len(rs) * 100
    return len(rs), statistics.mean(rs), statistics.median(rs), win


def _table(title, buckets, key, data, unit=""):
    print(f"\n── {title} ──")
    print(f"{'bucket':>12} | {'n':>3} | {'avg':>7} | {'median':>7} | {'win%':>5}")
    for lo, hi, label in buckets:
        sub = [d for d in data if d.get(key) is not None and lo <= d[key] < hi]
        if len(sub) < MIN_N:
            print(f"{label:>12} | {len(sub):>3} |   (too few)")
            continue
        n, avg, med, win = _agg(sub)
        print(f"{label:>12} | {n:>3} | {_fmt_pct(avg)} | {_fmt_pct(med)} | {win:4.0f}%")


def _momentum_exit_backtest(data, series_by_pos, consecutive: int):
    """What each position WOULD have returned if sold the first time its momentum
    broke, instead of being held to its real exit.

    Motivated by the exit-reason table: scheduled exits (pre_catalyst) are the only
    profitable ones, while pain-triggered exits (disaster_stop at ~-40%, time_limit
    at ~-12%) produce nearly all the losses. So the question is whether the daily
    `climbing` flag — recorded for every position since 2026-08-10 but never wired
    to a decision — would have got us out earlier and cheaper.

    `consecutive` requires that many straight NOT-climbing observations before
    selling, so a single flickering day doesn't eject a position. A position whose
    momentum never broke is held to its actual exit (hypothetical == actual), which
    keeps the comparison honest rather than only counting the cases it helps.
    Exit slippage is charged on the hypothetical so it isn't flattered."""
    rows = []
    for p in data:
        series = series_by_pos.get(p["id"]) or []
        if not series:
            continue
        # Only positions whose capture starts near entry. Otherwise a momentum break
        # may have happened BEFORE we were recording, and we would miss it and
        # wrongly credit the strategy with holding on.
        lag = (date.fromisoformat(series[0]["obs_date"])
               - date.fromisoformat(p["entry_date"])).days
        if not (0 <= lag <= ENTRY_OBS_MAX_LAG_DAYS):
            continue
        streak, hypo = 0, None
        for m in series:
            if m.get("climbing") is False:
                streak += 1
                if streak >= consecutive and m.get("pnl_pct") is not None:
                    hypo = float(m["pnl_pct"]) - SLIPPAGE_PCT
                    break
            else:
                streak = 0
        cost = float(p["entry_price_aud"]) * int(p["quantity"])
        actual = p["pnl_pct"]
        rows.append({
            "ticker": p["ticker"], "fired": hypo is not None,
            "actual_pct": actual, "hypo_pct": actual if hypo is None else hypo,
            "actual_aud": cost * actual / 100.0,
            "hypo_aud": cost * (actual if hypo is None else hypo) / 100.0,
        })
    return rows


def run() -> None:
    closed = [p for p in store.get_closed_positions() if p.get("pnl_pct") is not None]
    if not closed:
        print("No closed sleeve positions yet.")
        return

    # --- Exclude the churn artefacts -------------------------------------------
    # A dated biotech bought INSIDE its own pre-catalyst window was sold straight back
    # out next run (the ACW.AX bug: four closes on four consecutive days, three of them
    # phantom). Those rows are real in the table but describe a defect, not a decision,
    # so they would poison every bucket below. Fixed at the entry gate on 2026-10-14;
    # excluded here so historical data stays usable.
    def _is_churn(p):
        if p.get("exit_reason") != "pre_catalyst" or not p.get("exit_date"):
            return False
        held = (date.fromisoformat(p["exit_date"]) - date.fromisoformat(p["entry_date"])).days
        return held <= PRE_CATALYST_EXIT_DAYS

    churn = [p for p in closed if _is_churn(p)]
    data = [p for p in closed if not _is_churn(p)]

    print(f"\n=== Sleeve entry-quality analysis ===")
    print(f"{len(closed)} closed positions"
          + (f"; EXCLUDED {len(churn)} churn artefact(s) "
             f"({', '.join(sorted({p['ticker'] for p in churn}))}) "
             f"worth ${sum(float(p['pnl_aud'] or 0) for p in churn):+.2f} AUD"
             if churn else "; no churn artefacts"))

    for p in data:
        p["pnl_pct"] = float(p["pnl_pct"])
        p["market_cap"] = float(p["market_cap"]) if p.get("market_cap") else None
        # Business days from entry to the catalyst — "was there an event close enough
        # to run into?" CRDL's was ~7-8 weeks out; names left waiting months faded.
        p["cat_prox_bd"] = None
        if p.get("catalyst_date") and p.get("entry_date"):
            try:
                p["cat_prox_bd"] = business_days_until(
                    date.fromisoformat(p["catalyst_date"]), date.fromisoformat(p["entry_date"]))
            except ValueError:
                pass

    # --- Attach the entry-day momentum reading ---------------------------------
    # Momentum capture began 2026-08-10, so anything opened earlier has no entry-day
    # row at all. Its EARLIEST row would be a mid-life reading — which would silently
    # masquerade as an entry reading and bias the whole result — so only accept a row
    # within ENTRY_OBS_MAX_LAG_DAYS of entry.
    momentum_rows = store.get_momentum_for_positions([p["id"] for p in data])
    series_by_pos: dict[str, list] = {}
    for m in momentum_rows:
        series_by_pos.setdefault(m["position_id"], []).append(m)   # already oldest-first
    by_pos: dict[str, dict] = {}
    for m in momentum_rows:
        by_pos.setdefault(m["position_id"], m)   # oldest-first ordering => first wins
    for p in data:
        p["entry_ret_20d"] = None
        m = by_pos.get(p["id"])
        if m and m.get("ret_20d") is not None:
            lag = (date.fromisoformat(m["obs_date"]) - date.fromisoformat(p["entry_date"])).days
            if 0 <= lag <= ENTRY_OBS_MAX_LAG_DAYS:
                p["entry_ret_20d"] = float(m["ret_20d"])

    with_ext = [p for p in data if p["entry_ret_20d"] is not None]
    n, avg, med, win = _agg(data)
    print(f"{n} analysable | avg {_fmt_pct(avg)} median {_fmt_pct(med)} win {win:.0f}%")
    print(f"{len(with_ext)} of {n} have a usable entry-day momentum reading "
          f"(capture began 2026-08-10)")

    # --- 1. THE question: does buying extended predict losses? -----------------
    if len(with_ext) < MIN_N:
        print("\n── Entry extension ──\n  (too few instrumented positions yet — re-run later)")
    else:
        c = _pearson([p["entry_ret_20d"] for p in with_ext], [p["pnl_pct"] for p in with_ext])
        print(f"\n── Entry extension: 20d return AT ENTRY vs realized return ──")
        print(f"  corr = {c:+.2f}" if c is not None else "  corr = n/a")
        print("  (negative => the more extended at entry, the worse the outcome)")
        _table("Return by entry extension", EXT_BUCKETS, "entry_ret_20d", with_ext)

    # --- 2-4. The other candidate levers --------------------------------------
    _table("Return by market cap at entry", CAP_BUCKETS, "market_cap", data)
    _table("Return by catalyst proximity at entry", PROX_BUCKETS, "cat_prox_bd", data)

    print("\n── Return by vertical ──")
    print(f"{'vertical':>12} | {'n':>3} | {'avg':>7} | {'median':>7} | {'win%':>5}")
    for v in sorted({p["vertical"] for p in data}):
        sub = [p for p in data if p["vertical"] == v]
        if len(sub) < MIN_N:
            print(f"{v:>12} | {len(sub):>3} |   (too few)")
            continue
        n_, avg_, med_, win_ = _agg(sub)
        print(f"{v:>12} | {n_:>3} | {_fmt_pct(avg_)} | {_fmt_pct(med_)} | {win_:4.0f}%")

    # --- 5. How positions actually end ----------------------------------------
    print("\n── How positions end (exit reason) ──")
    print(f"{'reason':>22} | {'n':>3} | {'avg':>7} | {'total AUD':>10}")
    reasons = {}
    for p in data:
        reasons.setdefault(p.get("exit_reason") or "?", []).append(p)
    for r, sub in sorted(reasons.items(), key=lambda kv: sum(float(p["pnl_aud"] or 0) for p in kv[1])):
        tot = sum(float(p["pnl_aud"] or 0) for p in sub)
        print(f"{r:>22} | {len(sub):>3} | {_fmt_pct(statistics.mean([p['pnl_pct'] for p in sub]))} | {tot:>+10.2f}")

    # --- 6. Would a momentum-invalidation exit have done better? ---------------
    print("\n── BACKTEST: sell when momentum breaks, instead of holding to the stop ──")
    print("  (hypothetical is charged exit slippage; positions whose momentum never")
    print("   broke are held to their real exit, so this isn't cherry-picked)")
    print(f"{'rule':>18} | {'n':>3} | {'fired':>5} | {'actual':>9} | {'if exited':>9} | {'delta':>9}")
    any_rows = False
    for consecutive in (1, 2, 3):
        rows = _momentum_exit_backtest(data, series_by_pos, consecutive)
        if len(rows) < MIN_N:
            continue
        any_rows = True
        a = sum(r["actual_aud"] for r in rows)
        h = sum(r["hypo_aud"] for r in rows)
        fired = sum(1 for r in rows if r["fired"])
        label = f"{consecutive} day{'s' if consecutive > 1 else ''} broken"
        print(f"{label:>18} | {len(rows):>3} | {fired:>5} | {a:>+9.2f} | {h:>+9.2f} | {h-a:>+9.2f}")
    if not any_rows:
        print("  (too few positions with momentum captured from entry — re-run later)")
    print()


if __name__ == "__main__":
    run()
