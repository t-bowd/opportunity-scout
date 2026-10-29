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
import re
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

# --- Objective insider attributes (section 7) --------------------------------
# Every scored dimension has now been tested and none of them separates winners
# from losers: score ±0.0, conviction NEGATIVE (−0.05 at 30d, −0.17 at 90d), and
# the two instrumented loser-hypotheses (rescore_down, entry extension) both died.
# What has never been tested is the FACTUAL content of the filing itself, which
# collectors/edgar.py has been parsing and storing in signals.raw_data all along:
# who bought, how much they spent, and whether they bought alone. Those are facts
# about the insider's own behaviour rather than Gemini's opinion of the setup, so
# they are the one remaining feature family that could gate entries.
FORM4_SOURCE = "edgar_4"
CLUSTER_WINDOW_DAYS = 14   # matches db.client.count_recent_insider_buyers' default
VALUE_BUCKETS = [(0, 50e3, "<$50k"), (50e3, 250e3, "$50-250k"),
                 (250e3, 1e6, "$250k-1M"), (1e6, 1e15, ">$1M")]
CLUSTER_BUCKETS = [(1, 2, "1 buyer"), (2, 3, "2 buyers"), (3, 9999, "3+ buyers")]
ROLE_CLASSES = ["ceo_cfo", "other_officer", "director", "ten_pct_owner"]
# A C-suite buyer sees the whole business; a director sees the board pack. The
# officerTitle field is free text, so match on it rather than trusting isOfficer.
_SENIOR_TITLE_RE = re.compile(r"ceo|chief exec|cfo|chief financ|president|chairman|chair ", re.I)


def _role_class(roles) -> str | None:
    """Collapse a Form 4's role list to its most senior class, or None if unknown.

    edgar.py emits e.g. ["director"], ["CEO"], ["officer"], ["10% owner"], or a
    combination, and falls back to ["insider"] when the filing sets no flags — that
    fallback carries no information, so it maps to None rather than a bucket."""
    if not roles:
        return None
    lowered = [str(r).strip().lower() for r in roles]
    titles = [r for r in lowered if r not in ("director", "10% owner", "insider")]
    if any(_SENIOR_TITLE_RE.search(t) for t in titles):
        return "ceo_cfo"
    if titles:
        return "other_officer"
    if "director" in lowered:
        return "director"
    if "10% owner" in lowered:
        return "ten_pct_owner"
    return None


def _form4_signals() -> list[dict]:
    """Every stored Form 4 open-market-purchase signal, paged.

    Paged because a single .execute() caps at 1000 rows and the table is well past
    that — an unpaged read would silently truncate the cluster counts.
    """
    db = get_client()
    out, start, page = [], 0, 1000
    while True:
        rows = (db.table("signals").select("id, raw_data, signal_date")
                .eq("source", FORM4_SOURCE)
                .range(start, start + page - 1).execute().data)
        out.extend(rows)
        if len(rows) < page:
            return out
        start += page


def _attach_insider_attrs(data: list[dict]) -> int:
    """Attach buyer role, purchase size and cluster size to insider_buy records.

    Returns how many records got a usable Form 4 attached. Each opportunity stores
    the signal_ids it was built from; we take the LARGEST code-P purchase among them
    as the defining buy (a filing cluster scored as one opportunity is characterised
    by its biggest buyer, not an arbitrary one).
    """
    sigs = _form4_signals()
    by_id = {s["id"]: s for s in sigs}
    # ticker -> [(signal_date, buyer)] for counting distinct buyers in a window
    by_ticker: dict[str, list[tuple[str, str]]] = {}
    for s in sigs:
        rd = s.get("raw_data") or {}
        tkr, buyer = rd.get("ticker"), rd.get("buyer")
        if tkr and buyer and s.get("signal_date"):
            by_ticker.setdefault(tkr, []).append((s["signal_date"], buyer))

    attached = 0
    for d in data:
        d["role_class"] = d["buy_value_usd"] = d["cluster_n"] = None
        linked = [by_id[sid] for sid in (d.get("signal_ids") or []) if sid in by_id]
        if not linked:
            continue
        best = max(linked, key=lambda s: (s.get("raw_data") or {}).get("value_usd") or 0)
        rd = best.get("raw_data") or {}
        d["role_class"] = _role_class(rd.get("roles"))
        val = rd.get("value_usd")
        d["buy_value_usd"] = float(val) if val else None
        try:
            ref = date.fromisoformat(best["signal_date"])
            buyers = {b for sd, b in by_ticker.get(rd.get("ticker") or d["vehicle"], [])
                      if abs((date.fromisoformat(sd) - ref).days) <= CLUSTER_WINDOW_DAYS}
            d["cluster_n"] = len(buyers) or None
        except (ValueError, TypeError, KeyError):
            pass
        if d["role_class"] or d["buy_value_usd"] or d["cluster_n"]:
            attached += 1
    return attached


def _spy_series(lookback_days: int = 250) -> tuple[list, list]:
    """SPY daily closes as (sorted dates, closes). Default ~8 months.

    run_hold_analysis.py asks for a longer window: it benchmarks each pick over its
    own realised holding period, so it needs SPY back to the oldest ENTRY, not just
    the oldest matured return.
    """
    t2 = int(datetime.now(timezone.utc).timestamp())
    t1 = int((datetime.now(timezone.utc) - timedelta(days=lookback_days)).timestamp())
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
        "liquidity, timing, price_at_score, created_at, signal_ids, "
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
            "signal_ids": opp.get("signal_ids") or [],
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

    # 7. Objective insider attributes — the FACTS of the filing, not the score.
    print("\n── Objective Form 4 attributes (hypothesis: the filing's facts beat the score) ──")
    attached = _attach_insider_attrs(data)
    print(f"  {attached} of {len(data)} records have a linked Form 4 purchase")

    def _bucket_table(title, buckets, key, subset, fmt=lambda v: v):
        print(f"\n  {title}")
        print(f"{'bucket':>14} | {'n':>3} | {'avg':>7} | {'median':>7} | {'win%':>5} | {'avg α':>7}")
        for lo, hi, label in buckets:
            sub = [d for d in subset if d.get(key) is not None and lo <= d[key] < hi]
            if len(sub) < MIN_N:
                print(f"{label:>14} | {len(sub):>3} |   (too few)")
                continue
            n, avg, med, win, a = _agg(sub)
            print(f"{label:>14} | {n:>3} | {_fmt_pct(avg)} | {_fmt_pct(med)} | "
                  f"{win*100:4.0f}% | {_fmt_pct(a)}")

    if attached < MIN_N:
        print("  (too few linked filings — nothing to segment on yet)")
    else:
        print("\n  Return by buyer seniority")
        print(f"{'role':>14} | {'n':>3} | {'avg':>7} | {'median':>7} | {'win%':>5} | {'avg α':>7}")
        for rc in ROLE_CLASSES:
            sub = [d for d in data if d.get("role_class") == rc]
            if len(sub) < MIN_N:
                print(f"{rc:>14} | {len(sub):>3} |   (too few)")
                continue
            n, avg, med, win, a = _agg(sub)
            print(f"{rc:>14} | {n:>3} | {_fmt_pct(avg)} | {_fmt_pct(med)} | "
                  f"{win*100:4.0f}% | {_fmt_pct(a)}")

        _bucket_table("Return by purchase size (USD)", VALUE_BUCKETS, "buy_value_usd", data)
        _bucket_table(f"Return by buyer cluster (distinct buyers within "
                      f"±{CLUSTER_WINDOW_DAYS}d)", CLUSTER_BUCKETS, "cluster_n", data)

        print("\n  Correlations (continuous form of the same two attributes)")
        for key, label in (("buy_value_usd", "log10(purchase $)"), ("cluster_n", "cluster size")):
            pairs = [d for d in data if d.get(key)]
            if len(pairs) < MIN_N:
                print(f"{label:>18}:   n/a ({len(pairs)} rows)")
                continue
            import math
            xs = [math.log10(d[key]) if key == "buy_value_usd" else d[key] for d in pairs]
            c = _pearson(xs, [d["ret"] for d in pairs])
            print(f"{label:>18}: {c:+.2f}  (n={len(pairs)})" if c is not None
                  else f"{label:>18}:   n/a")
    print()


if __name__ == "__main__":
    run()
