"""
Exit-timing backtest — is the 60-day holding period too long?

Motivated by the first 90-day forward-return run (2026-10-29, n=129), which showed
the insider_buy edge DECAYING with time: alpha +2.5% at 30 days but −0.6% at 90.
The book holds to 60 days (90 with the reprieve), i.e. straight through the decay.

run_analysis.py can't answer what to do about that, because its 30d/90d numbers are
"hold unconditionally to day N" — they ignore the stops entirely. The real question
is what the book would have earned under ITS OWN exit rules with a shorter calendar
limit, since the stop, the breakeven ratchet and the trail all interact with the time
exit: shortening the limit cuts losers earlier but also ejects slow winners before
they can arm the trail.

So this walks each pick's actual daily price path and simulates the live ruleset:

  - vol-scaled hard stop (5×ATR clamped 10–18%, computed from bars BEFORE entry only)
  - breakeven ratchet once peak gain clears the position's own stop distance
  - +20% arm / 8% trail
  - calendar time exit at the swept limit, with the 30-day green-climbing reprieve

...once per limit in HOLD_SWEEP, on ONE fixed cohort of names so the limits are
compared on identical picks rather than on whatever happened to mature.

Fidelity notes, since this is meant to inform a real-money parameter:
  - Stops fill INTRADAY off the day's LOW, which is what a resting Alpaca stop does.
    Polling the close here would understate stop-outs — the exact paper-vs-live
    divergence that made the paper book look 2.5× better than live.
  - Stop/trail/ratchet state is decided from the PREVIOUS close's peak, because the
    daily run is what moves those orders. No same-bar look-ahead.
  - Entry and exit are both charged SLIPPAGE_PCT, so no limit is flattered.
  - A stop is assumed to fill AT its level. Real gaps fill worse, so the stop-heavy
    (short) limits are, if anything, flattered slightly.

Read-only. Decides nothing — same contract as run_analysis.py.

    python run_hold_analysis.py
"""
import statistics
import time
from datetime import date, timedelta, datetime, timezone

import requests

from db.client import get_client
from run_analysis import _spy_series, _spy_on
from paper_trader.entry import _atr_pct, _stop_pct_for, ATR_PERIOD, SLIPPAGE_PCT
from paper_trader.exit import (
    TRAILING_STOP_ACTIVATE_PCT,
    TRAILING_STOP_TRAIL_PCT,
    TIME_EXIT_REPRIEVE_DAYS,
    MAX_HOLD_DAYS_BY_PATTERN,
    _time_exit_reprieved,
)

# None = no calendar limit at all (stop/trail only) — the patience upper bound.
HOLD_SWEEP = [20, 30, 40, 50, 60, None]
MAX_SWEEP_DAYS = 60
# Every name must have enough path to see the LONGEST limit through, reprieve
# included, or a short limit would be scored on names a long limit never finishes.
REQUIRED_PATH_DAYS = MAX_SWEEP_DAYS + TIME_EXIT_REPRIEVE_DAYS
PRE_ENTRY_LOOKBACK_DAYS = 70   # calendar days of history before entry, for ATR
MIN_N = 10
REASONS = ["stop_loss", "breakeven_stop", "trailing_stop", "time_exit", "window_end"]


def _fetch_ohlc(ticker: str, t1: date, t2: date, attempts: int = 3) -> list[tuple]:
    """Daily (date, high, low, close) bars, oldest first. [] on any failure.

    Retries, because this is the first analysis to pull a per-ticker series for
    ~100 names from a CI runner. Yahoo throttles bursts from shared IPs, and a
    throttled ticker that silently became [] would drop out of the cohort —
    quietly changing which names the limits are compared on.
    """
    for attempt in range(attempts):
        bars = _fetch_ohlc_once(ticker, t1, t2)
        if bars:
            return bars
        if attempt < attempts - 1:
            time.sleep(1.5 * (attempt + 1))
    return []


def _fetch_ohlc_once(ticker: str, t1: date, t2: date) -> list[tuple]:
    p1 = int(datetime.combine(t1, datetime.min.time(), timezone.utc).timestamp())
    p2 = int(datetime.combine(t2, datetime.min.time(), timezone.utc).timestamp())
    try:
        url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
               f"?period1={p1}&period2={p2}&interval=1d")
        r = requests.get(url, headers={"User-Agent": "OpportunityScout"}, timeout=20)
        res = r.json()["chart"]["result"][0]
        q = res["indicators"]["quote"][0]
        bars = []
        for ts, hi, lo, cl in zip(res["timestamp"], q["high"], q["low"], q["close"]):
            if hi is None or lo is None or cl is None or cl <= 0:
                continue
            bars.append((datetime.fromtimestamp(ts, tz=timezone.utc).date(),
                         float(hi), float(lo), float(cl)))
        return bars
    except Exception:
        return []


def _simulate(entry_date: date, entry_price: float, stop_pct: float,
              bars: list[tuple], max_hold: int | None) -> dict:
    """Walk the path under the live exit rules. Returns the realised outcome.

    `bars` are strictly after the entry date, oldest first. `stop_pct` is a positive
    percent. Pure apart from the constants it reads — the logic mirrors
    paper_trader.exit.run_exits, which is why the helpers are imported rather than
    restated: if the live rules change, this backtest changes with them.
    """
    peak = entry_price          # peak through the PREVIOUS close; only ratchets up
    trail_armed = False

    def _out(px, reason, d):
        net = px * (1 - SLIPPAGE_PCT / 100)
        return {"ret": (net / entry_price - 1) * 100, "reason": reason,
                "exit_date": d, "days_held": (d - entry_date).days}

    for d, hi, lo, cl in bars:
        days_held = (d - entry_date).days
        peak_gain = (peak - entry_price) / entry_price * 100
        breakeven_armed = peak_gain >= stop_pct
        stop_level = (entry_price if breakeven_armed
                      else entry_price * (1 - stop_pct / 100))

        # 1. Resting orders fill intraday, before any calendar decision can run.
        if trail_armed:
            trail_level = peak * (1 - TRAILING_STOP_TRAIL_PCT / 100)
            if lo <= trail_level:
                return _out(trail_level, "trailing_stop", d)
        elif lo <= stop_level:
            return _out(stop_level,
                        "breakeven_stop" if breakeven_armed else "stop_loss", d)

        # 2. Calendar exit — a market sell at the close, and only for names the
        #    trail hasn't claimed and the reprieve doesn't spare.
        if (max_hold is not None and days_held >= max_hold and not trail_armed
                and not _time_exit_reprieved(days_held, max_hold, peak_gain, cl, peak)):
            return _out(cl, "time_exit", d)

        # 3. End-of-day state, applied from tomorrow (the daily run moves the orders).
        peak = max(peak, hi)
        if (peak - entry_price) / entry_price * 100 >= TRAILING_STOP_ACTIVATE_PCT:
            trail_armed = True

    d, _hi, _lo, cl = bars[-1]
    return _out(cl, "window_end", d)


def _load_cohort() -> list[dict]:
    """Scored picks old enough to have a full REQUIRED_PATH_DAYS of price path."""
    cutoff = (date.today() - timedelta(days=REQUIRED_PATH_DAYS + 5)).isoformat()
    db = get_client()
    rows = (db.table("opportunities")
            .select("vehicle, pattern, total_score, price_at_score, created_at")
            .lte("created_at", cutoff)
            .not_.is_("price_at_score", "null")
            .execute().data)
    best: dict[str, dict] = {}
    for o in rows:
        tkr, px, created = o.get("vehicle"), o.get("price_at_score"), o.get("created_at")
        if not tkr or not px or float(px) <= 0 or not created:
            continue
        rec = {"ticker": tkr, "pattern": o.get("pattern") or "?",
               "score": o.get("total_score") or 0, "price_at_score": float(px),
               "created": date.fromisoformat(created[:10])}
        # Dedupe by ticker, keeping the highest-scored occurrence — same convention
        # as run_analysis.py so the two reports describe the same universe.
        if tkr not in best or rec["score"] > best[tkr]["score"]:
            best[tkr] = rec
    return list(best.values())


def _agg(outcomes: list[dict]) -> tuple:
    rets = [o["ret"] for o in outcomes]
    alphas = [o["alpha"] for o in outcomes if o["alpha"] is not None]
    win = sum(1 for r in rets if r > 0) / len(rets) * 100
    return (len(rets), statistics.mean(rets), statistics.median(rets), win,
            statistics.mean(alphas) if alphas else None,
            statistics.median([o["days_held"] for o in outcomes]))


def _fmt(v):
    return "   n/a" if v is None else f"{v:+6.1f}%"


def _sweep_table(title: str, paths: list[dict], spy_dates, spy_closes) -> None:
    print(f"\n── {title} ──")
    hdr = (f"{'limit':>7} | {'n':>3} | {'avg':>7} | {'median':>7} | {'win%':>5} | "
           f"{'avg α':>7} | {'med d':>5} | " + " | ".join(f"{r[:5]:>5}" for r in REASONS))
    print(hdr)
    for max_hold in HOLD_SWEEP:
        outcomes = []
        for p in paths:
            o = _simulate(p["entry_date"], p["entry_price"], p["stop_pct"],
                          p["bars"], max_hold)
            s0 = _spy_on(spy_dates, spy_closes, p["entry_date"])
            s1 = _spy_on(spy_dates, spy_closes, o["exit_date"])
            o["alpha"] = (o["ret"] - (s1 / s0 - 1) * 100) if (s0 and s1 and s0 > 0) else None
            outcomes.append(o)
        if len(outcomes) < MIN_N:
            print(f"{str(max_hold):>7} | {len(outcomes):>3} |   (too few)")
            continue
        n, avg, med, win, alpha, medd = _agg(outcomes)
        counts = {r: sum(1 for o in outcomes if o["reason"] == r) for r in REASONS}
        label = "none" if max_hold is None else f"{max_hold}d"
        print(f"{label:>7} | {n:>3} | {_fmt(avg)} | {_fmt(med)} | {win:4.0f}% | "
              f"{_fmt(alpha)} | {medd:>5.0f} | "
              + " | ".join(f"{counts[r]:>5}" for r in REASONS))


def run() -> None:
    cohort = _load_cohort()
    print("\n=== Exit-timing backtest: is the 60-day hold too long? ===")
    print(f"{len(cohort)} deduped tickers scored on or before "
          f"{(date.today() - timedelta(days=REQUIRED_PATH_DAYS + 5)).isoformat()}")
    if not cohort:
        print("Nothing old enough to backtest yet.\n")
        return

    spy_dates, spy_closes = _spy_series(lookback_days=500)
    paths, skipped = [], {"no_bars": 0, "short_atr": 0, "short_path": 0}
    for i, c in enumerate(cohort):
        bars = _fetch_ohlc(c["ticker"],
                           c["created"] - timedelta(days=PRE_ENTRY_LOOKBACK_DAYS),
                           c["created"] + timedelta(days=REQUIRED_PATH_DAYS + 10))
        time.sleep(0.15)   # be polite to Yahoo across ~100+ tickers
        if not bars:
            skipped["no_bars"] += 1
            continue
        pre = [b for b in bars if b[0] <= c["created"]]
        post = [b for b in bars if b[0] > c["created"]]
        # ATR from pre-entry bars ONLY — no look-ahead into the window being scored.
        if len(pre) < ATR_PERIOD + 1:
            skipped["short_atr"] += 1
            continue
        if not post or (post[-1][0] - c["created"]).days < REQUIRED_PATH_DAYS:
            skipped["short_path"] += 1
            continue
        atr = _atr_pct([b[1] for b in pre], [b[2] for b in pre], [b[3] for b in pre])
        paths.append({
            **c,
            "entry_date": c["created"],
            "entry_price": c["price_at_score"] * (1 + SLIPPAGE_PCT / 100),
            "stop_pct": _stop_pct_for(atr),
            "atr_pct": atr,
            "bars": post,
        })
        if (i + 1) % 25 == 0:
            print(f"  …fetched {i + 1}/{len(cohort)}")

    print(f"{len(paths)} usable paths "
          f"(skipped: {skipped['no_bars']} no price data, "
          f"{skipped['short_atr']} too little pre-entry history, "
          f"{skipped['short_path']} path shorter than {REQUIRED_PATH_DAYS}d)")
    if len(paths) < MIN_N:
        print(f"Not enough usable paths (need ≥{MIN_N}).\n")
        return

    dates = sorted(p["entry_date"] for p in paths)
    stops = [p["stop_pct"] for p in paths]
    print(f"Entry dates {dates[0]} … {dates[-1]} | "
          f"simulated stop {min(stops):.1f}–{max(stops):.1f}% "
          f"(median {statistics.median(stops):.1f}%) | "
          f"{SLIPPAGE_PCT}% slippage charged both sides")
    print("Reason columns: which rule ended the trade. 'windo' = still open at the "
          f"{REQUIRED_PATH_DAYS}d edge of the window.")

    _sweep_table("All patterns", paths, spy_dates, spy_closes)

    # Per-pattern, because the limit is configured per pattern (MAX_HOLD_DAYS_BY_PATTERN)
    # and insider_buy is both the dominant pattern and the one that showed the decay.
    for pat in sorted({p["pattern"] for p in paths}):
        sub = [p for p in paths if p["pattern"] == pat]
        if len(sub) < MIN_N:
            continue
        cur = MAX_HOLD_DAYS_BY_PATTERN.get(pat)
        _sweep_table(f"{pat} only (currently {cur}d)", sub, spy_dates, spy_closes)

    print()


if __name__ == "__main__":
    run()
