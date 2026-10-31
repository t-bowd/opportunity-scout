"""
Exit backtest — the holding period, and the exit geometry around it.

PART 2 (added 2026-10-31) sweeps the exit GEOMETRY, because the September changes
left it internally inconsistent in two ways that only showed up once this backtest
existed:

  - Vol-scaled stops pushed the MEDIAN stop to 18% (the STOP_PCT_MAX ceiling), while
    the breakeven ratchet arms at 1.0x the stop distance and the trail takes over at
    +20%. So for half the book the ratchet operates across a ~2-point band of peak
    gain — near-dead code, on exactly the volatile names whose +15%-then-roll-over
    round trips it was built to stop.
  - The trail is still FLAT 8% while stops are vol-scaled, so it sits at ~4x ATR for
    a calm name but ~2.2x for a jumpy one: the same rule, twice as noise-sensitive on
    the names that actually move. And the hold sweep shows the trail is now the
    dominant exit for winners, so its width sets how much of each winner we keep.

Both are tested rather than argued about, including the real trade-off that arming
the ratchet earlier breaks the invariant that kept it safe (buffer from trigger down
to breakeven == the stop width the position was sized against).

PART 1 — is the 60-day holding period too long?

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
from run_analysis import _bench_series, _spy_on, BENCH_SPY, BENCH_IWM
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

# The live exit geometry — every sweep varies ONE of these and leaves the rest here,
# so each row is "what the book does today, except for this one knob".
LIVE_GEOMETRY = {
    "max_hold": MAX_HOLD_DAYS_BY_PATTERN.get("insider_buy", 60),
    "ratchet_mult": 1.0,
    "trail_arm": TRAILING_STOP_ACTIVATE_PCT,
    "trail_pct": TRAILING_STOP_TRAIL_PCT,
    "trail_atr_mult": None,
}
# Breakeven arms at ratchet_mult × the position's stop distance. Below 1.0 it arms
# before the position is up a full stop width — earlier protection, but it breaks the
# invariant that made the ratchet safe (buffer from trigger down to breakeven == the
# stop width it was sized against), so it can scratch winners on ordinary noise.
# That trade-off is exactly what wants measuring rather than arguing about.
RATCHET_SWEEP = [0.4, 0.5, 0.6, 0.75, 1.0, 1.25]
# Trail arm. Anything at or below STOP_PCT_MAX (18) leaves the ratchet no room at all.
TRAIL_ARM_SWEEP = [15.0, 20.0, 25.0, 30.0, 40.0]
TRAIL_PCT_SWEEP = [6.0, 8.0, 10.0, 12.0, 15.0, 20.0]
# Vol-scaled trail: trail = k × the name's own ATR, the way the STOP already works.
TRAIL_ATR_SWEEP = [1.5, 2.0, 2.5, 3.0, 4.0, 5.0]


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
              bars: list[tuple], max_hold: int | None,
              ratchet_mult: float = 1.0,
              trail_arm: float = TRAILING_STOP_ACTIVATE_PCT,
              trail_pct: float = TRAILING_STOP_TRAIL_PCT,
              trail_atr_mult: float | None = None,
              atr_pct: float | None = None) -> dict:
    """Walk the path under the live exit rules. Returns the realised outcome.

    `bars` are strictly after the entry date, oldest first. `stop_pct` is a positive
    percent. The logic mirrors paper_trader.exit.run_exits, which is why the helpers
    are imported rather than restated: if the live rules change, this changes with
    them.

    The four geometry knobs all DEFAULT to the live settings, so an unparameterised
    call reproduces the book exactly. They exist because the September changes left
    the exit geometry internally inconsistent:

      ratchet_mult   — breakeven arms at ratchet_mult × stop_pct. Live is 1.0, which
                       for a median 18% stop means arming at +18% while the trail
                       takes over at +20%: a 2-point band in which the ratchet can
                       act at all, i.e. near-dead code for half the book.
      trail_arm      — raising it above STOP_PCT_MAX gives the ratchet real room.
      trail_pct      — flat 8% live, which is ~4× ATR for a calm name but only ~2.2×
                       for a jumpy one, so the same rule is twice as noise-sensitive
                       on exactly the names that move.
      trail_atr_mult — if set, the trail becomes trail_atr_mult × the name's own ATR
                       (floored at 4% so it never sits inside the spread), making the
                       trail vol-scaled the way the STOP already is.
    """
    peak = entry_price          # peak through the PREVIOUS close; only ratchets up
    trail_armed = False
    ratchet_at = ratchet_mult * stop_pct
    if trail_atr_mult is not None and atr_pct:
        trail_pct = max(4.0, trail_atr_mult * atr_pct)

    def _out(px, reason, d):
        net = px * (1 - SLIPPAGE_PCT / 100)
        return {"ret": (net / entry_price - 1) * 100, "reason": reason,
                "exit_date": d, "days_held": (d - entry_date).days}

    for d, hi, lo, cl in bars:
        days_held = (d - entry_date).days
        peak_gain = (peak - entry_price) / entry_price * 100
        breakeven_armed = peak_gain >= ratchet_at
        stop_level = (entry_price if breakeven_armed
                      else entry_price * (1 - stop_pct / 100))

        # 1. Resting orders fill intraday, before any calendar decision can run.
        if trail_armed:
            trail_level = peak * (1 - trail_pct / 100)
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
        if (peak - entry_price) / entry_price * 100 >= trail_arm:
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


def _fmt(v):
    return "   n/a" if v is None else f"{v:+6.1f}%"


def _mean_or_none(vals):
    clean = [v for v in vals if v is not None]
    return statistics.mean(clean) if clean else None


def _evaluate(paths: list[dict], benches: dict, **geo) -> dict:
    """Simulate every path under one geometry and aggregate. `geo` goes to _simulate."""
    outcomes = []
    for p in paths:
        o = _simulate(p["entry_date"], p["entry_price"], p["stop_pct"], p["bars"],
                      atr_pct=p["atr_pct"], **geo)
        for name, (bd, bc) in benches.items():
            b0 = _spy_on(bd, bc, p["entry_date"])
            b1 = _spy_on(bd, bc, o["exit_date"])
            o[f"a_{name}"] = (o["ret"] - (b1 / b0 - 1) * 100) if (b0 and b1 and b0 > 0) else None
        outcomes.append(o)
    rets = [o["ret"] for o in outcomes]
    return {
        "n": len(rets), "avg": statistics.mean(rets), "med": statistics.median(rets),
        "win": sum(1 for r in rets if r > 0) / len(rets) * 100,
        "a_spy": _mean_or_none([o["a_SPY"] for o in outcomes]),
        "a_iwm": _mean_or_none([o["a_IWM"] for o in outcomes]),
        "a_iwm_mo": _alpha_per_month(outcomes, "a_IWM"),
        "med_days": statistics.median([o["days_held"] for o in outcomes]),
        "counts": {r: sum(1 for o in outcomes if o["reason"] == r) for r in REASONS},
    }


def _alpha_per_month(outcomes: list[dict], key: str) -> float | None:
    """Alpha per 30 days of time-in-market, aggregated over the book.

    Per-TRADE alpha mechanically rewards holding longer: if the average pick beats
    the benchmark at all, a config that holds 40 days accrues more of that edge than
    one holding 16, and the wider-trail rows look better for a reason that has
    nothing to do with better exit timing. With a hard slot cap that is the wrong
    comparison — a slot held 40 days cannot hold anything else — so the decision
    metric is alpha per position-DAY.

    Computed as total alpha / total days rather than the mean of per-trade ratios,
    because a 3-day stop-out would otherwise annualise into a meaningless −180%.
    """
    pairs = [(o[key], o["days_held"]) for o in outcomes
             if o.get(key) is not None and o["days_held"] > 0]
    if not pairs:
        return None
    total_days = sum(d for _a, d in pairs)
    return (sum(a for a, _d in pairs) / total_days * 30) if total_days else None


def _hdr(label: str, width: int = 7) -> None:
    print(f"{label:>{width}} | {'n':>3} | {'avg':>7} | {'median':>7} | {'win%':>5} | "
          f"{'α SPY':>7} | {'α IWM':>7} | {'α/mo':>7} | {'med d':>5} | "
          + " | ".join(f"{r[:5]:>5}" for r in REASONS))


def _print_row(label: str, s: dict, width: int = 7, mark: str = "") -> None:
    print(f"{label:>{width}} | {s['n']:>3} | {_fmt(s['avg'])} | {_fmt(s['med'])} | "
          f"{s['win']:4.0f}% | {_fmt(s['a_spy'])} | {_fmt(s['a_iwm'])} | "
          f"{_fmt(s['a_iwm_mo'])} | {s['med_days']:>5.0f} | "
          + " | ".join(f"{s['counts'][r]:>5}" for r in REASONS) + mark)


def _sweep_table(title: str, paths: list[dict], benches: dict) -> None:
    """The original question: how long to hold."""
    print(f"\n── {title} ──")
    _hdr("limit")
    for max_hold in HOLD_SWEEP:
        if len(paths) < MIN_N:
            print(f"{str(max_hold):>7} | {len(paths):>3} |   (too few)")
            continue
        label = "none" if max_hold is None else f"{max_hold}d"
        mark = "  <= LIVE" if max_hold == LIVE_GEOMETRY["max_hold"] else ""
        _print_row(label, _evaluate(paths, benches, max_hold=max_hold,
                                    **{k: v for k, v in LIVE_GEOMETRY.items()
                                       if k != "max_hold"}), mark=mark)


def _knob_table(title: str, knob: str, values: list, paths: list[dict],
                benches: dict, col: str, note: str = "") -> None:
    """Vary ONE exit-geometry knob with everything else at the live setting."""
    print(f"\n── {title} ──")
    if note:
        print(f"  {note}")
    _hdr(col)
    live_val = LIVE_GEOMETRY.get(knob)
    for v in values:
        mark = "  <= LIVE" if v == live_val else ""
        _print_row(f"{v:g}", _evaluate(paths, benches, **{**LIVE_GEOMETRY, knob: v}),
                   mark=mark)


def _grid_table(paths: list[dict], benches: dict) -> None:
    """Every combination of the geometry knobs, ranked.

    Reported with the live config's RANK and the grid's own spread, because the
    best cell of a 400-cell grid on ~80 names is partly luck. The question this
    answers is not "which cell wins" but "is the surface flat?" — if the top cells
    barely beat the median, the geometry is not where the money is and nothing
    should change. Only a wide spread AND a readable gradient justify a real change.
    """
    print("\n── FULL GRID: every geometry combination, ranked by α IWM PER MONTH ──")
    print("  (per-month, not per-trade: a slot held twice as long must earn twice as "
          "much to be worth it)")
    results = []
    # The vol-scaled variants keep the live flat width as their trail_pct: _simulate
    # only overrides it when the name HAS an ATR, so this is the fallback for a name
    # whose ATR failed — not 0, which would exit on the first tick down.
    trail_variants = ([{"trail_pct": t, "trail_atr_mult": None} for t in TRAIL_PCT_SWEEP]
                      + [{"trail_pct": LIVE_GEOMETRY["trail_pct"], "trail_atr_mult": k}
                         for k in TRAIL_ATR_SWEEP])
    for rm in RATCHET_SWEEP:
        for arm in TRAIL_ARM_SWEEP:
            for tv in trail_variants:
                geo = {"max_hold": LIVE_GEOMETRY["max_hold"], "ratchet_mult": rm,
                       "trail_arm": arm, **tv}
                s = _evaluate(paths, benches, **geo)
                trail_lbl = (f"{tv['trail_atr_mult']:g}xATR"
                             if tv["trail_atr_mult"] is not None else f"{tv['trail_pct']:g}%")
                is_live = (rm == LIVE_GEOMETRY["ratchet_mult"]
                           and arm == LIVE_GEOMETRY["trail_arm"]
                           and tv["trail_atr_mult"] is LIVE_GEOMETRY["trail_atr_mult"]
                           and tv["trail_pct"] == LIVE_GEOMETRY["trail_pct"])
                results.append({"label": f"r{rm:g} arm{arm:g} trail{trail_lbl}",
                                "stats": s, "live": is_live,
                                "key": s["a_iwm_mo"] if s["a_iwm_mo"] is not None else -999})
    results.sort(key=lambda r: -r["key"])
    keys = [r["key"] for r in results]
    print(f"  {len(results)} combinations | α IWM/month spread "
          f"{min(keys):+.1f}% … {max(keys):+.1f}% | median {statistics.median(keys):+.1f}%")
    live = next((i for i, r in enumerate(results) if r["live"]), None)
    if live is not None:
        print(f"  LIVE config ranks {live + 1} of {len(results)} "
              f"(α IWM/month {results[live]['key']:+.1f}%)")
    _hdr("config", 26)
    for r in results[:8]:
        _print_row(r["label"], r["stats"], 26, "  <= LIVE" if r["live"] else "")
    if live is not None and live >= 8:
        print(f"{'  … (live config) …':>26}")
        _print_row(results[live]["label"], results[live]["stats"], 26, "  <= LIVE")
    print(f"{'  … worst …':>26}")
    _print_row(results[-1]["label"], results[-1]["stats"], 26)


def run() -> None:
    cohort = _load_cohort()
    print("\n=== Exit backtest: holding period + exit geometry ===")
    print(f"{len(cohort)} deduped tickers scored on or before "
          f"{(date.today() - timedelta(days=REQUIRED_PATH_DAYS + 5)).isoformat()}")
    if not cohort:
        print("Nothing old enough to backtest yet.\n")
        return

    benches = {BENCH_SPY: _bench_series(BENCH_SPY, lookback_days=500),
               BENCH_IWM: _bench_series(BENCH_IWM, lookback_days=500)}
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

    print("α SPY = vs large caps (historical). α IWM = vs small caps, the universe "
          "we actually buy from — judge changes on this one.")

    # --- PART 1: how long to hold ------------------------------------------------
    _sweep_table("HOLD PERIOD — all patterns", paths, benches)
    for pat in sorted({p["pattern"] for p in paths}):
        sub = [p for p in paths if p["pattern"] == pat]
        if len(sub) < MIN_N:
            continue
        cur = MAX_HOLD_DAYS_BY_PATTERN.get(pat)
        _sweep_table(f"HOLD PERIOD — {pat} only (currently {cur}d)", sub, benches)

    # --- PART 2: the exit GEOMETRY -----------------------------------------------
    # Tuned on insider_buy ONLY. smart_money is code-gated out of the live book, so
    # including it would tune the exits on names real money never buys.
    live_pats = [p for p in paths if p["pattern"] == "insider_buy"]
    if len(live_pats) < MIN_N:
        print(f"\n(only {len(live_pats)} insider_buy paths — skipping the geometry sweeps)")
        print()
        return

    print(f"\n{'=' * 78}")
    print(f"EXIT GEOMETRY — swept on the {len(live_pats)} insider_buy paths "
          f"(what the live book actually buys)")
    print(f"Live today: ratchet at {LIVE_GEOMETRY['ratchet_mult']:g}x stop, "
          f"trail arms +{LIVE_GEOMETRY['trail_arm']:g}%, trails "
          f"{LIVE_GEOMETRY['trail_pct']:g}% flat, {LIVE_GEOMETRY['max_hold']}d limit")
    print("=" * 78)

    med_stop = statistics.median([p["stop_pct"] for p in live_pats])
    _knob_table(
        "BREAKEVEN RATCHET trigger (x the position's own stop distance)",
        "ratchet_mult", RATCHET_SWEEP, live_pats, benches, "xStop",
        note=(f"At 1.0x with a median {med_stop:.0f}% stop the ratchet arms at "
              f"+{med_stop:.0f}% while the trail takes over at "
              f"+{LIVE_GEOMETRY['trail_arm']:g}% — a "
              f"{LIVE_GEOMETRY['trail_arm'] - med_stop:.0f}pt window in which it can act "
              "at all. Lower = arms earlier = more scratches, fewer full losses."))
    _knob_table(
        "TRAIL ARM (peak gain % at which the trailing stop takes over)",
        "trail_arm", TRAIL_ARM_SWEEP, live_pats, benches, "arm%",
        note="Raising this above the 18% stop ceiling is what gives the ratchet room.")
    _knob_table(
        "TRAIL WIDTH, flat (% below peak)",
        "trail_pct", TRAIL_PCT_SWEEP, live_pats, benches, "trail%",
        note="Flat today, so it is ~2x more noise-sensitive on jumpy names than calm ones.")
    _knob_table(
        "TRAIL WIDTH, vol-scaled (x the name's own ATR, floored at 4%)",
        "trail_atr_mult", TRAIL_ATR_SWEEP, live_pats, benches, "xATR",
        note="The direct test: does matching the trail to the name's own volatility "
             "beat one flat number? Compare against the flat table above.")

    _grid_table(live_pats, benches)
    print()


if __name__ == "__main__":
    run()
