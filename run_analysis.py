"""
Forward-return analysis over the feedback dataset — the report that turns
"every scored pick + its 30/90d price" into scoring-refinement signals.

Answers, from realised forward returns (price_at_score → price_30d/90d):
  1. Does a higher total_score actually earn a higher return?   (by score bucket)
  2. Which patterns pay, and which don't?                       (by pattern)
  3. Which score dimensions carry the edge?                     (dim ↔ return corr)
  4. Is the full-book slot cap costing us?                      (entered vs blocked)

Returns are reported both raw and ALPHA (return minus SPY over the same window),
so we measure edge, not market drift. Rows are deduped by ticker (keep the
highest-scored occurrence) so legacy duplicate opportunities don't skew the stats.

Read-only. Run on demand, or wire into the weekly digest once the sample matures.

    python run_analysis.py            # 30d analysis (default)
    python run_analysis.py --90d      # use 90d returns where available
"""
import sys
import statistics
from bisect import bisect_left
from datetime import date, timedelta, datetime, timezone

import requests
from db.client import get_client

HORIZON = 90 if "--90d" in sys.argv else 30
MIN_N = 3  # don't report a bucket/pattern with fewer than this many datapoints

SCORE_BUCKETS = [(0, 12, "≤12"), (13, 14, "13-14"),
                 (15, 16, "15-16"), (17, 18, "17-18"), (19, 20, "19-20")]


def _spy_series() -> tuple[list, list]:
    """SPY daily closes for the last ~8 months as (sorted dates, closes)."""
    t2 = int(datetime.now(timezone.utc).timestamp())
    t1 = int((datetime.now(timezone.utc) - timedelta(days=250)).timestamp())
    try:
        url = (f"https://query1.finance.yahoo.com/v8/finance/chart/SPY"
               f"?period1={t1}&period2={t2}&interval=1d")
        r = requests.get(url, headers={"User-Agent": "OpportunityScout"}, timeout=15)
        res = r.json()["chart"]["result"][0]
        dates, closes = [], []
        for ts, c in zip(res["timestamp"], res["indicators"]["quote"][0]["close"]):
            if c is not None:
                dates.append(datetime.fromtimestamp(ts, tz=timezone.utc).date())
                closes.append(c)
        return dates, closes
    except Exception:
        return [], []


def _spy_on(dates, closes, target: date):
    """SPY close on the trading day nearest `target`, or None."""
    if not dates:
        return None
    i = bisect_left(dates, target)
    cands = [j for j in (i - 1, i, i + 1) if 0 <= j < len(dates)]
    if not cands:
        return None
    best = min(cands, key=lambda j: abs((dates[j] - target).days))
    return closes[best]


def _pearson(xs, ys):
    if len(xs) < 3:
        return None
    try:
        return statistics.correlation(xs, ys)
    except Exception:
        return None


def _fmt_pct(v):
    return f"{v*100:+6.1f}%" if v is not None else "   n/a"


def run() -> None:
    db = get_client()
    rows = db.table("feedback").select(
        "acted, price_30d, price_90d, "
        "opportunities(vehicle, pattern, total_score, conviction, asymmetry, "
        "liquidity, timing, price_at_score, created_at, "
        "rescore_action, ret_20d_at_score)"
    ).execute().data

    price_col = "price_90d" if HORIZON == 90 else "price_30d"
    spy_dates, spy_closes = _spy_series()

    # Build records with a realised forward return + market-relative alpha.
    recs = {}   # vehicle -> best record (highest total_score)
    for r in rows:
        opp = r.get("opportunities") or {}
        ref, fwd = opp.get("price_at_score"), r.get(price_col)
        tkr = opp.get("vehicle")
        created_raw = opp.get("created_at", "")
        if not (tkr := tkr) or not ref or not fwd or not created_raw:
            continue
        ref, fwd = float(ref), float(fwd)
        if ref <= 0:
            continue
        ret = fwd / ref - 1
        created = date.fromisoformat(created_raw[:10])
        alpha = None
        s0 = _spy_on(spy_dates, spy_closes, created)
        s1 = _spy_on(spy_dates, spy_closes, created + timedelta(days=HORIZON))
        if s0 and s1 and s0 > 0:
            alpha = ret - (s1 / s0 - 1)
        rec = {
            "vehicle": tkr, "pattern": opp.get("pattern") or "?",
            "score": opp.get("total_score") or 0,
            "conviction": opp.get("conviction") or 0, "asymmetry": opp.get("asymmetry") or 0,
            "liquidity": opp.get("liquidity") or 0, "timing": opp.get("timing") or 0,
            "acted": bool(r.get("acted")), "ret": ret, "alpha": alpha,
            "rescore_action": opp.get("rescore_action"),
            "ret_20d_at_score": (float(opp["ret_20d_at_score"])
                                 if opp.get("ret_20d_at_score") is not None else None),
        }
        # Dedupe by ticker: keep the highest-scored occurrence.
        if tkr not in recs or rec["score"] > recs[tkr]["score"]:
            recs[tkr] = rec

    data = list(recs.values())
    print(f"\n=== Forward-return analysis ({HORIZON}d) ===")
    print(f"{len(rows)} feedback rows → {len(data)} tickers with a matured {HORIZON}d return "
          f"(deduped by ticker){'  [SPY alpha unavailable]' if not spy_dates else ''}\n")
    if len(data) < MIN_N:
        print(f"Not enough matured data yet (need ≥{MIN_N}). Re-run once more picks cross {HORIZON} days.\n")
        return

    def _agg(subset):
        rets = [d["ret"] for d in subset]
        alphas = [d["alpha"] for d in subset if d["alpha"] is not None]
        win = sum(1 for x in rets if x > 0) / len(rets)
        return (len(rets), statistics.mean(rets), statistics.median(rets),
                win, (statistics.mean(alphas) if alphas else None))

    # 1. By score bucket -----------------------------------------------------
    print("── Return by score bucket ──")
    print(f"{'bucket':>7} | {'n':>3} | {'avg':>7} | {'median':>7} | {'win%':>5} | {'avg α':>7}")
    for lo, hi, label in SCORE_BUCKETS:
        sub = [d for d in data if lo <= d["score"] <= hi]
        if len(sub) < MIN_N:
            print(f"{label:>7} | {len(sub):>3} |   (too few)")
            continue
        n, avg, med, win, a = _agg(sub)
        print(f"{label:>7} | {n:>3} | {_fmt_pct(avg)} | {_fmt_pct(med)} | {win*100:4.0f}% | {_fmt_pct(a)}")

    # 2. By pattern ----------------------------------------------------------
    print("\n── Return by pattern ──")
    print(f"{'pattern':>14} | {'n':>3} | {'avg':>7} | {'median':>7} | {'win%':>5} | {'avg α':>7}")
    patterns = sorted({d["pattern"] for d in data})
    for p in sorted(patterns, key=lambda p: -statistics.mean([d["ret"] for d in data if d["pattern"] == p])):
        sub = [d for d in data if d["pattern"] == p]
        if len(sub) < MIN_N:
            continue
        n, avg, med, win, a = _agg(sub)
        print(f"{p:>14} | {n:>3} | {_fmt_pct(avg)} | {_fmt_pct(med)} | {win*100:4.0f}% | {_fmt_pct(a)}")

    # 3. Dimension ↔ return correlation --------------------------------------
    print("\n── Which score dimension predicts return? (Pearson corr with return) ──")
    for dim in ("conviction", "asymmetry", "liquidity", "timing", "score"):
        c = _pearson([d[dim] for d in data], [d["ret"] for d in data])
        bar = ""
        if c is not None:
            bar = ("+" if c >= 0 else "-") * min(20, int(abs(c) * 20))
        print(f"{dim:>11}: {c:+.2f} {bar}" if c is not None else f"{dim:>11}:   n/a")

    # 4. Entered vs blocked (opportunity cost of the slot cap) ----------------
    print("\n── Entered vs blocked (is the slot cap costing us?) ──")
    for label, flag in (("entered", True), ("blocked", False)):
        sub = [d for d in data if d["acted"] is flag]
        if len(sub) < MIN_N:
            print(f"{label:>8}: {len(sub)} (too few)")
            continue
        n, avg, med, win, a = _agg(sub)
        print(f"{label:>8}: n={n:>3}  avg {_fmt_pct(avg)}  median {_fmt_pct(med)}  win {win*100:4.0f}%  α {_fmt_pct(a)}")

    # 5. Rescore-at-entry: does a name marked DOWN on its scoring day underperform?
    # (Instrumented migration 005; only rows scored after that fill in — NULL before.)
    print("\n── Return by rescore-at-entry (hypothesis: rescore_down = already fading) ──")
    _has_rescore = [d for d in data if d.get("rescore_action")]
    if len(_has_rescore) < MIN_N:
        print(f"  (only {len(_has_rescore)} instrumented rows — accruing since migration 005; re-check later)")
    else:
        for act in ("insert", "rescore_up", "rescore_down"):
            sub = [d for d in _has_rescore if d["rescore_action"] == act]
            if len(sub) < MIN_N:
                print(f"{act:>13} | {len(sub):>3} |   (too few)")
                continue
            n, avg, med, win, a = _agg(sub)
            print(f"{act:>13} | {n:>3} | {_fmt_pct(avg)} | {_fmt_pct(med)} | {win*100:4.0f}% | {_fmt_pct(a)}")

    # 6. Entry extension: does buying a name already run-up (high trailing 20d) revert?
    print("\n── Return by entry extension (trailing 20d at score; hypothesis: extended = reverts) ──")
    _has_ext = [d for d in data if d.get("ret_20d_at_score") is not None]
    if len(_has_ext) < MIN_N:
        print(f"  (only {len(_has_ext)} instrumented rows — accruing since migration 005; re-check later)")
    else:
        EXT_BUCKETS = [(-9.9, 0.0, "≤0%"), (0.0, 0.20, "0-20%"),
                       (0.20, 0.50, "20-50%"), (0.50, 9.9, ">50%")]
        c = _pearson([d["ret_20d_at_score"] for d in _has_ext], [d["ret"] for d in _has_ext])
        print(f"  corr(trailing-20d, forward-{HORIZON}d return): {c:+.2f}" if c is not None else "  corr: n/a")
        for lo, hi, label in EXT_BUCKETS:
            sub = [d for d in _has_ext if lo <= d["ret_20d_at_score"] < hi]
            if len(sub) < MIN_N:
                print(f"{label:>7} | {len(sub):>3} |   (too few)")
                continue
            n, avg, med, win, a = _agg(sub)
            print(f"{label:>7} | {n:>3} | {_fmt_pct(avg)} | {_fmt_pct(med)} | {win*100:4.0f}% | {_fmt_pct(a)}")
    print()


if __name__ == "__main__":
    run()
