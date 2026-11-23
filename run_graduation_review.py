"""
The graduation review, as a script rather than a hand calculation.

    python run_graduation_review.py

CONTEXT.md defines graduation as "20 closed trades with positive expectancy,
before any real capital". That gate has been overtaken by events: real money is
already deployed, so the question is no longer "may we start?" but "does the
real-money record justify continuing?" — which is a different question with a
different sample. paper_trader/snapshot.py counts the live book against
GRADUATION_MIN_LIVE_CLOSES and raises the flag; this is what to run when it does.

It is deliberately a script and not a chat answer. The last review was assembled
by hand from Actions logs, which took an afternoon, could not be re-run, and got
the headline number wrong (it assumed the broker account had never been topped
up). Every figure below is read from the database or the broker, so the next
review is one command and the answers are comparable with this one.

Read-only: it touches no orders and writes no rows.
"""
from dotenv import load_dotenv

load_dotenv(".env.local")

import statistics
from collections import defaultdict
from datetime import date

from db.client import get_client
from broker import execution as broker_execution
from paper_trader.snapshot import GRADUATION_MIN_LIVE_CLOSES

# Reused rather than reimplemented: these already handle the awkward parts (gap
# days, weekend entry dates, missing bars) and are smoke-tested where it matters.
from run_analysis import _bench_series, _spy_on, _pearson, BENCH_SPY, BENCH_IWM

FX = "AUDUSD=X"


# --- helpers -------------------------------------------------------------------

def _f(v, default=None):
    """float() that tolerates postgrest's strings and NULLs."""
    if v is None or v == "":
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _d(v):
    return date.fromisoformat(v) if v else None


def _pct(v, width=6):
    return f"{v:+{width}.1f}%" if v is not None else "    n/a"


def _money(v):
    return f"${v:+,.0f}"


def _stats(pnls: list[float]) -> str:
    if not pnls:
        return "none"
    wins = sum(1 for p in pnls if p > 0)
    return (f"{len(pnls):3}x {wins:2}w  {_money(sum(pnls)):>9}  "
            f"exp {sum(pnls) / len(pnls):+7.2f}  win {wins / len(pnls) * 100:3.0f}%")


def _mean(vals):
    vals = [v for v in vals if v is not None]
    return statistics.mean(vals) if vals else None


def _median(vals):
    vals = [v for v in vals if v is not None]
    return statistics.median(vals) if vals else None


def _hr(title: str) -> None:
    print(f"\n{'─' * 78}\n{title}\n{'─' * 78}")


# --- data ----------------------------------------------------------------------

def _closed_positions() -> list[dict]:
    """Every closed position, with the scoring attributes of the pick behind it.

    Manual closes are interventions, not strategy outcomes, so they are excluded
    here exactly as snapshot.py excludes them — otherwise a decision to flush a
    position lands in the record as a strategy result.
    """
    db = get_client()
    rows = db.table("paper_positions").select(
        "ticker, entry_date, exit_date, entry_price_aud, exit_price_aud, quantity, "
        "brokerage_aud, pnl_aud, pnl_pct, exit_reason, status, broker, stop_loss_pct, "
        "opportunities(pattern, total_score, conviction, asymmetry, ret_20d_at_score)"
    ).execute().data or []

    out = []
    for r in rows:
        # Filtered here rather than in the query: the table is small enough that a
        # round trip isn't worth a postgrest NULL-filter idiom getting it wrong
        # silently and quietly dropping closes from the record.
        if not r.get("exit_date") or r.get("pnl_aud") is None:
            continue
        if r.get("status") == "closed_manual":
            continue
        opp = r.get("opportunities") or {}
        entry, exit_ = _d(r.get("entry_date")), _d(r.get("exit_date"))
        out.append({
            "ticker": r["ticker"],
            "entry": entry,
            "exit": exit_,
            "days": (exit_ - entry).days if entry and exit_ else None,
            "pnl_aud": _f(r["pnl_aud"], 0.0),
            "pnl_pct": _f(r.get("pnl_pct")),
            "reason": r.get("exit_reason") or "unknown",
            "live": r.get("broker") == "alpaca",
            # NULL stop_loss_pct is the legacy flat -12% era (migration 006) — the
            # cleanest available marker for "before vol-scaled stops shipped".
            "stop_pct": _f(r.get("stop_loss_pct")),
            "score": _f(opp.get("total_score")),
            "pattern": opp.get("pattern"),
            "ret_20d": _f(opp.get("ret_20d_at_score")),
            "us": not r["ticker"].endswith(".AX"),
        })
    out.sort(key=lambda p: (p["exit"] or date.min, p["ticker"]))
    return out


def bench_leg_aud(b0, b1, fx0, fx1) -> float | None:
    """An AUD investor's % return from holding the index over the same window.

    AUD in, USD out, AUD back — the currency move rides on the benchmark leg
    exactly as it rides on the position, so what's left over is stock selection
    and not the Australian dollar. fx is AUDUSD, so a FALLING fx means a weaker
    AUD, which lifts the AUD value of a US holding: hence fx0/fx1, not fx1/fx0.

    Getting this inverted is a live risk rather than a theoretical one — it turns
    a currency move into alpha and reads as skill. Hence a pure function with a
    smoke test instead of four inline terms.
    """
    if not b0 or not b1 or not fx0 or not fx1:
        return None
    return ((b1 / b0) * (fx0 / fx1) - 1) * 100


def _attach_alpha(positions: list[dict]) -> None:
    """Per-position alpha over each position's OWN window, for US names.

    The benchmark leg carries the same currency move as the position (AUD in,
    USD out, AUD back), because pnl_pct is an AUD figure — comparing it against a
    raw USD index return would score the Australian dollar as stock selection.
    """
    spy_d, spy_c = _bench_series(BENCH_SPY, lookback_days=400)
    iwm_d, iwm_c = _bench_series(BENCH_IWM, lookback_days=400)
    fx_d, fx_c = _bench_series(FX, lookback_days=400)

    for p in positions:
        p["alpha_spy"] = p["alpha_iwm"] = p["bench_spy"] = None
        if not (p["us"] and p["entry"] and p["exit"] and p["pnl_pct"] is not None):
            continue
        fx0, fx1 = _spy_on(fx_d, fx_c, p["entry"]), _spy_on(fx_d, fx_c, p["exit"])
        if not fx0 or not fx1:
            continue
        for name, (bd, bc) in (("spy", (spy_d, spy_c)), ("iwm", (iwm_d, iwm_c))):
            bench = bench_leg_aud(_spy_on(bd, bc, p["entry"]),
                                  _spy_on(bd, bc, p["exit"]), fx0, fx1)
            if bench is None:
                continue
            p[f"bench_{name}"] = bench
            p[f"alpha_{name}"] = p["pnl_pct"] - bench


# --- sections ------------------------------------------------------------------

def _section_capital(live_closes: list[dict]) -> None:
    _hr("1. RETURN ON CAPITAL — the figure the decision turns on")
    inception = broker_execution.account_since_inception_usd()
    if inception is None:
        print("broker unreachable — cannot read the account's own record.")
        print("Everything below is the trade ledger, which cannot see deposits.")
        return
    if inception["return_pct"] is None:
        print("no capital base to measure against.")
        return

    print(f"funded {inception['start_date']}, {inception['days']}d ago")
    print(f"  ${inception['start_equity']:,.2f} initial"
          + (f"  {inception['net_flows_usd']:+,.2f} transferred later"
             if abs(inception["net_flows_usd"]) >= 1 else ""))
    print(f"  base ${inception['invested_usd']:,.2f}  ->  now ${inception['equity']:,.2f}"
          f"   = {inception['return_pct']:+.1f}%"
          f"   ({_money(inception['equity'] - inception['invested_usd'])})")

    spy_d, spy_c = _bench_series(BENCH_SPY, lookback_days=400)
    iwm_d, iwm_c = _bench_series(BENCH_IWM, lookback_days=400)
    start = _d(inception["start_date"])
    print("\nsame window, buy-and-hold instead:")
    for name, (bd, bc) in ((BENCH_SPY, (spy_d, spy_c)), (BENCH_IWM, (iwm_d, iwm_c))):
        b0, b1 = _spy_on(bd, bc, start), (bc[-1] if bc else None)
        if not b0 or not b1:
            continue
        bench = (b1 / b0 - 1) * 100
        print(f"  {name:4} {bench:+6.1f}%   ->  alpha "
              f"{inception['return_pct'] - bench:+6.1f}%")

    if abs(inception["net_flows_usd"]) >= 1:
        print("\n  NOTE: the base assumes every transfer was present from day one,")
        print("  which damps the percentage toward zero — so on a loss this figure")
        print("  is the CEILING on the real return, not the floor. The dollar")
        print("  figure is exact regardless.")


def _section_ledger(positions: list[dict]) -> None:
    _hr("2. THE REALISED LEDGER — live vs the simulation that authorised it")
    for label, subset in (("LIVE (real money)", [p for p in positions if p["live"]]),
                          ("PAPER (simulated)", [p for p in positions if not p["live"]])):
        print(f"\n{label}: {_stats([p['pnl_aud'] for p in subset])}")
        by_reason = defaultdict(list)
        for p in subset:
            by_reason[p["reason"]].append(p)
        for reason, group in sorted(by_reason.items(),
                                    key=lambda kv: sum(p["pnl_aud"] for p in kv[1])):
            print(f"  {reason:14} {_stats([p['pnl_aud'] for p in group])}"
                  f"  avg {_pct(_mean([p['pnl_pct'] for p in group]))}"
                  f"  median {_median([p['days'] for p in group]):.0f}d")


def _section_trend(live: list[dict]) -> None:
    _hr("3. IS IT IMPROVING? — live closes split chronologically")
    if len(live) < 6:
        print("too few live closes to split.")
        return
    mid = len(live) // 2
    for label, half in (("first half", live[:mid]), ("second half", live[mid:])):
        span = f"{half[0]['exit']} .. {half[-1]['exit']}"
        print(f"  {label:12} {span}  {_stats([p['pnl_aud'] for p in half])}")
    print("\n  (exit-date order, so the second half carries the September changes)")


def _section_stops(live: list[dict]) -> None:
    _hr("4. THE STOP, BEFORE AND AFTER VOL-SCALING")
    stops = [p for p in live if p["reason"] == "stop_loss"]
    if not stops:
        print("no live stops.")
        return
    for label, group in (("flat -12% (legacy)", [p for p in stops if p["stop_pct"] is None]),
                         ("vol-scaled 5xATR", [p for p in stops if p["stop_pct"] is not None])):
        if not group:
            print(f"  {label:20} none")
            continue
        print(f"  {label:20} {_stats([p['pnl_aud'] for p in group])}"
              f"  avg {_pct(_mean([p['pnl_pct'] for p in group]))}"
              f"  median {_median([p['days'] for p in group]):.0f}d")
        set_at = [p["stop_pct"] for p in group if p["stop_pct"] is not None]
        if set_at:
            ceiling = sum(1 for s in set_at if s >= 17.9)
            print(f"  {'':20} stop set at median {_median(set_at):.1f}%, "
                  f"{ceiling}/{len(set_at)} pinned at the 18% ceiling")
    # The question the whole book turns on: stops are the drawdown, so what does
    # everything that was NOT stopped out actually earn?
    ran = [p for p in live if p["reason"] != "stop_loss"]
    if ran:
        print(f"\n  everything NOT stopped out: {_stats([p['pnl_aud'] for p in ran])}")


def _section_alpha(live: list[dict]) -> None:
    _hr("5. PER-POSITION ALPHA — did the picks beat the market while held?")
    scored = [p for p in live if p["alpha_spy"] is not None]
    if not scored:
        print("no benchmark data.")
        return
    print(f"  all {len(scored):3} closed   "
          f"ret {_pct(_mean([p['pnl_pct'] for p in scored]))}   "
          f"SPY {_pct(_mean([p['bench_spy'] for p in scored]))}   "
          f"alpha SPY {_pct(_mean([p['alpha_spy'] for p in scored]))}   "
          f"IWM {_pct(_mean([p['alpha_iwm'] for p in scored]))}")
    by_reason = defaultdict(list)
    for p in scored:
        by_reason[p["reason"]].append(p)
    for reason, group in sorted(by_reason.items()):
        print(f"  {reason:14} {len(group):3}   "
              f"ret {_pct(_mean([p['pnl_pct'] for p in group]))}   "
              f"SPY {_pct(_mean([p['bench_spy'] for p in group]))}   "
              f"alpha SPY {_pct(_mean([p['alpha_spy'] for p in group]))}   "
              f"IWM {_pct(_mean([p['alpha_iwm'] for p in group]))}")
    print("\n  Per-position alpha and return on capital answer different questions:")
    print("  this one resets with every entry and forgets the ones that left.")


def _section_entry_hypothesis(live: list[dict]) -> None:
    _hr("6. THE ENTRY HYPOTHESIS — buying something that already ran")
    have = [p for p in live if p["ret_20d"] is not None and p["pnl_pct"] is not None]
    if len(have) < 8:
        print(f"only {len(have)} closed live positions carry ret_20d_at_score "
              "(instrumented from mid-September) — too few to read.")
        return
    xs = [p["ret_20d"] * 100 for p in have]
    ys = [p["alpha_iwm"] if p["alpha_iwm"] is not None else p["pnl_pct"] for p in have]
    r = _pearson(xs, ys)
    shown = f"{r:+.2f}" if r is not None else "n/a"
    print(f"  n={len(have)}  corr(prior 20d return, alpha) = {shown}")
    buckets = (("fell or flat", lambda x: x <= 0),
               ("up 0-10%", lambda x: 0 < x <= 10),
               ("up 10-20%", lambda x: 10 < x <= 20),
               ("up >20%", lambda x: x > 20))
    for label, test in buckets:
        group = [p for p, x in zip(have, xs) if test(x)]
        if not group:
            continue
        wins = sum(1 for p in group if p["pnl_aud"] > 0)
        print(f"  {label:14} n={len(group):3}  "
              f"ret {_pct(_mean([p['pnl_pct'] for p in group]))}  "
              f"alpha IWM {_pct(_mean([p['alpha_iwm'] for p in group]))}  "
              f"win {wins / len(group) * 100:3.0f}%")


def _section_worst(live: list[dict]) -> None:
    _hr("7. WHERE THE MONEY WENT — every live close, worst first")
    print(f"  {'ticker':8} {'exit':11} {'reason':14} {'days':>5} {'ret':>8} "
          f"{'$AUD':>9} {'aIWM':>8} {'score':>5} {'20d':>7}")
    for p in sorted(live, key=lambda p: p["pnl_aud"]):
        print(f"  {p['ticker']:8} {str(p['exit']):11} {p['reason']:14} "
              f"{p['days'] or 0:5} {_pct(p['pnl_pct'], 7)} {p['pnl_aud']:+9.2f} "
              f"{_pct(p['alpha_iwm'], 7)} "
              f"{p['score'] or 0:5.0f} "
              f"{_pct(p['ret_20d'] * 100, 6) if p['ret_20d'] is not None else '    n/a'}")


def run() -> None:
    print(f"=== GRADUATION REVIEW — {date.today().isoformat()} ===")
    positions = _closed_positions()
    _attach_alpha(positions)
    live = [p for p in positions if p["live"]]

    print(f"\n{len(live)} real-money closes (gate {GRADUATION_MIN_LIVE_CLOSES}), "
          f"{len(positions) - len(live)} simulated")

    _section_capital(live)
    _section_ledger(positions)
    _section_trend(live)
    _section_stops(live)
    _section_alpha(live)
    _section_entry_hypothesis(live)
    _section_worst(live)
    print()


if __name__ == "__main__":
    run()
