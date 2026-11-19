"""
Computes and saves a daily portfolio snapshot.
Runs after exits have been processed.
"""

import bisect
from datetime import date, datetime, timezone

import requests

from broker import execution as broker_execution
from db.client import (
    get_open_paper_positions,
    get_closed_paper_positions,
    upsert_paper_snapshot,
)
from paper_trader.exit import _fetch_price, _fetch_fx_rate

# The graduation gate is REAL-MONEY closes, not total closes. The sim book's
# record answers "does the strategy work in simulation", which is a different
# question — and counting every close against 20 made this a dead instrument:
# it passed 20 weeks ago and printed "0 trades until graduation review" on
# every run after that. Count the live book, and keep saying something true
# once the gate is passed.
GRADUATION_MIN_LIVE_CLOSES = 15


def _daily_closes(symbol: str) -> list[tuple[date, float]]:
    """Sorted (date, close) for the last year, dropping gap days. [] on failure."""
    try:
        url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
               "?interval=1d&range=1y")
        resp = requests.get(url, headers={"User-Agent": "OpportunityScout"}, timeout=10)
        result = resp.json()["chart"]["result"][0]
        ts = result["timestamp"]
        closes = result["indicators"]["quote"][0]["close"]
        hist = [(datetime.fromtimestamp(t, tz=timezone.utc).date(), c)
                for t, c in zip(ts, closes) if c is not None]
        hist.sort()
        return hist
    except Exception:
        return []


def _close_on_or_before(hist: list[tuple[date, float]], target: date) -> float | None:
    """The close on `target`, or the most recent trading day before it."""
    if not hist:
        return None
    i = bisect.bisect_right([d for d, _ in hist], target) - 1
    return hist[i][1] if i >= 0 else None


def _book_vs_spy(open_pos: list[dict], fx_rate: float,
                 spy_hist=None, audusd_hist=None, spy_now=None) -> tuple[float, float] | None:
    """
    Capital-weighted "vs market" read for a set of open positions: their
    mark-to-market AUD return, and the return an AUD investor would have made
    buying SPY on each position's own entry date instead. FX is applied to the SPY
    leg too (AUD->USD in, USD->AUD out), so US names and their SPY counterfactual
    carry the same currency move and the comparison is apples-to-apples. Returns
    (book_pct, spy_pct) or None if data is thin. SPY/FX history can be passed in to
    avoid refetching when scoring several sub-books in one run.
    """
    if not open_pos:
        return None
    spy_hist = spy_hist if spy_hist is not None else _daily_closes("SPY")
    audusd_hist = audusd_hist if audusd_hist is not None else _daily_closes("AUDUSD=X")
    spy_now = spy_now if spy_now is not None else _fetch_price("SPY")
    if not spy_hist or not spy_now or not fx_rate:
        return None

    cost_sum = 0.0        # AUD cost basis (weight)
    value_sum = 0.0       # AUD current mark
    spy_value_sum = 0.0   # AUD value if that cost had gone into SPY at entry
    for pos in open_pos:
        entry_date = date.fromisoformat(pos["entry_date"])
        qty = pos["quantity"]
        cost_aud = float(pos["entry_price_aud"]) * qty
        price = _fetch_price(pos["ticker"])
        spy_entry = _close_on_or_before(spy_hist, entry_date)
        audusd_entry = _close_on_or_before(audusd_hist, entry_date)
        if not price or not spy_entry or not audusd_entry or cost_aud <= 0:
            continue  # exclude from BOTH legs so weights stay aligned

        if pos["ticker"].endswith(".AX"):
            value_aud = price * qty
        else:
            value_aud = price / fx_rate * qty   # USD -> AUD, matches exit.py

        # AUD investor's SPY return over the same window (see docstring).
        spy_ret = (spy_now / spy_entry) * (audusd_entry / fx_rate)

        cost_sum += cost_aud
        value_sum += value_aud
        spy_value_sum += cost_aud * spy_ret

    if cost_sum <= 0:
        return None
    book_pct = (value_sum / cost_sum - 1) * 100
    spy_pct = (spy_value_sum / cost_sum - 1) * 100
    return book_pct, spy_pct


def _deployed_aud(positions: list[dict]) -> float:
    return sum(float(p["entry_price_aud"]) * p["quantity"] + float(p.get("brokerage_aud", 0))
               for p in positions)


def _print_realised(label: str, closes: list[dict]) -> None:
    """The realised record, split by HOW each position left the book.

    The split is the part that carries the finding. A book whose losses are all
    stops is a different problem from one whose losses are all time exits, and
    the totals hide which one you have — deriving it by hand from the run logs
    is what this line exists to stop.

    Worst reason first, so the line leads with where the money went.
    """
    if not closes:
        return
    pnls = [float(p["pnl_aud"]) for p in closes]
    total = sum(pnls)
    wins = sum(1 for x in pnls if x > 0)
    print(f"[paper/snapshot] {label} realised: {len(pnls)} closed, "
          f"{wins / len(pnls) * 100:.0f}% win, ${total:+.0f} total, "
          f"expectancy ${total / len(pnls):+.2f}")

    by_reason: dict[str, list[float]] = {}
    for pos, pnl in zip(closes, pnls):
        by_reason.setdefault(pos.get("exit_reason") or "unknown", []).append(pnl)
    split = "  ".join(
        f"{reason} {len(v)}× {sum(1 for x in v if x > 0)}w ${sum(v):+.0f}"
        for reason, v in sorted(by_reason.items(), key=lambda kv: sum(kv[1]))
    )
    print(f"[paper/snapshot] {label} by exit: {split}")


def _bench_return_pct(hist: list[tuple[date, float]], now_price: float | None,
                      start: date) -> float | None:
    """Simple price return of a benchmark from `start` to today, in its own currency."""
    entry = _close_on_or_before(hist, start)
    if not entry or not now_price:
        return None
    return (now_price / entry - 1) * 100


def _print_since_inception(label: str, inception: dict,
                           benchmarks: list[tuple[str, float | None]],
                           fx_rate: float | None) -> None:
    """Total return on the capital actually put into the account.

    Every other read in this file is per-position alpha, which answers "did each
    pick beat the market while we held it". It cannot answer "where would I be if
    I'd just bought the index", because a book of individually-respectable
    positions can still have lost money overall — the losers leave the book and
    stop being counted, the benchmark comparison resets with each new entry, and
    the open-book line only ever describes the survivors. That gap is exactly
    where a strategy hides, and it's the number the graduation decision turns on.

    USD, not AUD. The account is denominated in USD and no conversion ever
    happened, so an AUD figure would fold the currency's move into the strategy's
    record and call it performance. Today's AUD value is printed alongside for
    reconciliation against the rest of the book, nothing more.
    """
    pct = inception.get("return_pct")
    if pct is None:
        return      # withdrawn to nothing — no capital base to divide by
    invested = inception["invested_usd"]
    equity = inception["equity"]

    # With no transfers the percentage is exact. With them, daily bars don't say
    # WHEN the cash landed, so the denominator assumes it was there from day one:
    # that understates a late deposit's return and overstates a late withdrawal's.
    # The dollar profit is exact either way, so lead with it when it's in doubt.
    flows = inception["net_flows_usd"]
    moved = f" ({flows:+,.0f} USD net transfers)" if abs(flows) >= 1 else ""
    caveat = (f" (profit ${equity - invested:+,.0f}; % approximate — transfer timing unknown)"
              if abs(flows) >= 1 else "")
    aud = f" (${equity / fx_rate:,.0f} AUD)" if fx_rate else ""

    print(f"[paper/snapshot] {label} on capital: ${inception['start_equity']:,.0f} in"
          f"{moved} -> ${equity:,.0f} USD{aud} = {pct:+.1f}%{caveat} "
          f"since {inception['start_date']} ({inception['days']}d)")

    usable = [(name, b) for name, b in benchmarks if b is not None]
    if not usable:
        return
    print(f"[paper/snapshot] {label} vs market over the same days: "
          + "  ".join(f"{name} {b:+.1f}%" for name, b in usable)
          + "  ->  alpha "
          + "  ".join(f"{name} {pct - b:+.1f}%" for name, b in usable))


def run_snapshot() -> None:
    today = date.today()
    open_pos = get_open_paper_positions()
    closed_pos = get_closed_paper_positions()

    deployed_aud = sum(
        float(p["entry_price_aud"]) * p["quantity"] + float(p.get("brokerage_aud", 0))
        for p in open_pos
    )

    # Manual closes are interventions (e.g. flushing a position opened on a
    # mislabeled signal), not strategy outcomes — exclude them from the
    # performance sample so they don't pollute expectancy / win rate / graduation.
    strategy_closes = [
        p for p in closed_pos
        if p.get("pnl_aud") is not None and p.get("status") != "closed_manual"
    ]
    closed_pnls = [float(p["pnl_aud"]) for p in strategy_closes]
    total_pnl = sum(closed_pnls)
    wins = sum(1 for pnl in closed_pnls if pnl > 0)

    expectancy = total_pnl / len(closed_pnls) if closed_pnls else None
    win_rate = wins / len(closed_pnls) * 100 if closed_pnls else None

    snap = {
        "snapshot_date": today.isoformat(),
        "open_positions": len(open_pos),
        "total_deployed_aud": round(deployed_aud, 2),
        "closed_trades": len(closed_pnls),
        "winning_trades": wins,
        "total_pnl_aud": round(total_pnl, 2),
        "expectancy_aud": round(expectancy, 2) if expectancy is not None else None,
        "win_rate": round(win_rate, 2) if win_rate is not None else None,
    }

    upsert_paper_snapshot(snap)

    exp_str = f"${expectancy:+.2f}" if expectancy is not None else "n/a"
    wr_str = f"{win_rate:.0f}%" if win_rate is not None else "n/a"

    print(
        f"[paper/snapshot] {len(open_pos)} open, "
        f"${deployed_aud:.0f} deployed, "
        f"{len(closed_pnls)} closed "
        f"(expectancy {exp_str}, win rate {wr_str})"
    )

    # Portfolio-level "vs market" read. Once real money is live, split the open
    # book into LIVE (Alpaca, broker='alpaca') and PAPER (the legacy sim book
    # winding down) so the real-money performance isn't buried in the legacy
    # figure. Before cutover (no live positions) keep the single combined line.
    fx = _fetch_fx_rate()
    spy_hist = _daily_closes("SPY")
    audusd_hist = _daily_closes("AUDUSD=X")
    spy_now = _fetch_price("SPY")

    def _print_book(label: str, positions: list[dict]) -> None:
        if not positions:
            return
        line = f"[paper/snapshot] {label}: {len(positions)} open, ${_deployed_aud(positions):.0f} deployed"
        bench = _book_vs_spy(positions, fx, spy_hist, audusd_hist, spy_now)
        if bench:
            book_pct, spy_pct = bench
            line += f", {book_pct:+.1f}% vs SPY {spy_pct:+.1f}% -> alpha {book_pct - spy_pct:+.1f}%"
        print(line)

    live_pos = [p for p in open_pos if p.get("broker") == "alpaca"]
    paper_pos = [p for p in open_pos if p.get("broker") != "alpaca"]
    if live_pos:
        _print_book("LIVE (real money)", live_pos)
        _print_book("PAPER (winding down)", paper_pos)
    else:
        # No real money yet — unchanged single "open book" line.
        bench = _book_vs_spy(open_pos, fx, spy_hist, audusd_hist, spy_now)
        if bench:
            book_pct, spy_pct = bench
            print(f"[paper/snapshot] open book {book_pct:+.1f}% vs SPY {spy_pct:+.1f}% "
                  f"(same entries, AUD) -> alpha {book_pct - spy_pct:+.1f}%")

    # The realised record, and the gate that actually decides graduation.
    live_closes = [p for p in strategy_closes if p.get("broker") == "alpaca"]
    _print_realised("LIVE (real money)", live_closes)

    # Total return on the money actually put in. The per-position lines above can
    # read respectably while the account is down — this is the line that says so,
    # and the one the graduation review should turn on. IWM as well as SPY: the
    # book skews small, so SPY alone flatters or punishes it for size, not skill.
    inception = broker_execution.account_since_inception_usd()
    if inception:
        start = date.fromisoformat(inception["start_date"])
        _print_since_inception("LIVE (real money)", inception, [
            ("SPY", _bench_return_pct(spy_hist, spy_now, start)),
            ("IWM", _bench_return_pct(_daily_closes("IWM"), _fetch_price("IWM"), start)),
        ], fx)

    if len(live_closes) >= GRADUATION_MIN_LIVE_CLOSES:
        print(f"[paper/snapshot] ⚑ GRADUATION REVIEW DUE — {len(live_closes)} "
              f"real-money closes (gate {GRADUATION_MIN_LIVE_CLOSES})")
    else:
        print(f"[paper/snapshot] {GRADUATION_MIN_LIVE_CLOSES - len(live_closes)} "
              f"more real-money closes until graduation review "
              f"({len(live_closes)}/{GRADUATION_MIN_LIVE_CLOSES})")
