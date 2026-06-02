"""
Evaluates recent top opportunities (last 10 days) and opens paper positions.
Runs daily after scoring.

Entry filters (all must pass):
  1.  Score ≥ 13/20 (raised to 15 in a bearish market regime). Lowered from 16
      once liquidity stopped being a heavy score penalty; the market-cap floor
      that guards quality now lives at score time in analyzer/score.py.
  2.  Underlying signal filed within tiered recency window for the pattern
  3.  Fewer than 5 open positions
  4.  No duplicate ticker already open
  5.  Not already entered for this exact opportunity
  6.  Price fetchable from Yahoo Finance
  7.  Price has not moved >8% since scoring (avoids chasing)
  8.  Not within 7 days of scheduled earnings (avoids binary event risk)
  9.  Relative volume ≥ 1.5× for news/thematic signals, ≥ 0.8× for EDGAR signals
      (EDGAR filings are historical — volume spike may have already passed)
  10. Position size yields at least 1 share at $200 AUD target
"""

import requests
from datetime import date, datetime, timedelta, timezone

from db.client import (
    get_recent_opportunities,
    get_open_paper_positions,
    insert_paper_position,
    insert_paper_skipped,
    paper_position_exists_for_opportunity,
    auto_fill_feedback_entry,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RECENCY_WINDOWS: dict[str, int] = {
    "s1_filed":        2,
    "activist":        2,
    "insider_buy":     5,
    "smart_money":     5,
    "thematic_etf":    5,
    "etf_launch":      5,
    "pre_ipo_proxy":   5,
    "spin_off":        7,
    "index_inclusion": 7,
}
DEFAULT_RECENCY = 5

# Score gate. Lowered from 16 once liquidity stopped being a heavy score penalty
# (see analyzer/score.py): a quality small-cap insider buy now lands ~13-15, and
# at 16 those were being filtered out entirely — the main reason the pool was dry.
# The full stack of downstream entry filters (recency, volume, earnings blackout,
# price-move, dollar-volume floor at score time) does the quality work.
MIN_SCORE = 13
BEARISH_MIN_SCORE = 15          # higher bar when broad market is in drawdown
MARKET_REGIME_THRESHOLD = 0.90  # >10% below 52w high = bearish
MAX_POSITIONS = 5
POSITION_SIZE_AUD = 200.0
MAX_PRICE_MOVE_PCT = 8.0
EARNINGS_BLACKOUT_DAYS = 7
SLIPPAGE_PCT = 0.5
US_BROKERAGE_AUD = 1.0
ASX_BROKERAGE_AUD = 0.0

# Volume thresholds by pattern type.
# EDGAR signals (insider buys, 13F, S-1, activist) are historical filings —
# the volume spike often happens before we process them or not at all.
# News and thematic signals are current, so the market should react same-day.
EDGAR_PATTERNS = {"insider_buy", "smart_money", "s1_filed", "activist", "etf_launch", "spin_off"}
MIN_RELATIVE_VOLUME_NEWS = 1.5   # news/thematic: must be 1.5× avg
MIN_RELATIVE_VOLUME_EDGAR = 0.8  # EDGAR: just needs some activity

HEADERS = {"User-Agent": "OpportunityScout"}

# ---------------------------------------------------------------------------
# Helpers — all fail open (return None/False) so data issues don't block trades
# ---------------------------------------------------------------------------

def _fetch_fx_rate() -> float:
    """USD per 1 AUD. Falls back to 0.65 on failure."""
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/AUDUSD=X?interval=1d&range=5d"
        resp = requests.get(url, headers=HEADERS, timeout=10)
        return resp.json()["chart"]["result"][0]["meta"]["regularMarketPrice"]
    except Exception:
        print("[paper/entry] FX rate fetch failed, using fallback 0.65")
        return 0.65


def _fetch_price_and_earnings(ticker: str) -> tuple[float | None, int | None]:
    """
    Returns (current_price, days_to_earnings).
    days_to_earnings is None if unknown, negative if already passed.
    """
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=5d"
        resp = requests.get(url, headers=HEADERS, timeout=10)
        meta = resp.json()["chart"]["result"][0]["meta"]
        price = meta.get("regularMarketPrice")
        ts = meta.get("earningsTimestamp")
        days = None
        if ts:
            earnings_date = datetime.fromtimestamp(ts, tz=timezone.utc).date()
            days = (earnings_date - date.today()).days
        return price, days
    except Exception:
        return None, None


def _fetch_relative_volume(ticker: str) -> float | None:
    """Today's volume / 20-day average. Returns None if unavailable."""
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=1mo"
        resp = requests.get(url, headers=HEADERS, timeout=10)
        volumes = resp.json()["chart"]["result"][0]["indicators"]["quote"][0].get("volume", [])
        volumes = [v for v in volumes if v is not None]
        if len(volumes) < 5:
            return None
        avg = sum(volumes[:-1]) / len(volumes[:-1])
        return volumes[-1] / avg if avg > 0 else None
    except Exception:
        return None


def _market_is_bearish(is_asx: bool) -> bool:
    """True if the relevant index is >10% below its 52-week high."""
    index = "^AXJO" if is_asx else "SPY"
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{index}?interval=1d&range=1y"
        resp = requests.get(url, headers=HEADERS, timeout=10)
        data = resp.json()["chart"]["result"][0]
        closes = [c for c in data["indicators"]["quote"][0].get("close", []) if c]
        price = data["meta"].get("regularMarketPrice")
        if closes and price:
            return price < max(closes) * MARKET_REGIME_THRESHOLD
    except Exception:
        pass
    return False  # fail open


def _recency_ok(opp: dict) -> tuple[bool, str]:
    """True if the opportunity is fresh enough for its pattern type."""
    pattern = opp.get("pattern", "")
    window = RECENCY_WINDOWS.get(pattern, DEFAULT_RECENCY)
    cutoff = date.today() - timedelta(days=window)
    created_str = opp.get("created_at", "")
    if not created_str:
        return True, ""
    try:
        scored_on = date.fromisoformat(created_str[:10])
        if scored_on < cutoff:
            return False, f"stale:{pattern}_window_{window}d_scored_{scored_on}"
    except Exception:
        pass
    return True, ""


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_entries(week_of: str | None = None) -> None:
    if week_of is None:
        today = date.today()
        week_of = (today - timedelta(days=today.weekday())).isoformat()

    # Pull from the last 10 days rather than the current calendar week so a
    # Friday-scored opportunity is still actionable on Monday. Per-pattern
    # recency windows below still gate how fresh each individual pick must be.
    opportunities = get_recent_opportunities(days=10, limit=25)
    open_positions = get_open_paper_positions()
    open_count = len(open_positions)
    open_tickers = {p["ticker"] for p in open_positions}

    if open_count >= MAX_POSITIONS:
        print(f"[paper/entry] {open_count} positions open — no slots, skipping")
        return

    fx_rate = _fetch_fx_rate()

    # Market regime — compute once per run for each market
    bearish_us = _market_is_bearish(is_asx=False)
    bearish_asx = _market_is_bearish(is_asx=True)
    if bearish_us:
        print(f"[paper/entry] US market regime: BEARISH — min score raised to {BEARISH_MIN_SCORE}")
    if bearish_asx:
        print(f"[paper/entry] ASX market regime: BEARISH — min score raised to {BEARISH_MIN_SCORE}")

    entered = 0

    for opp in opportunities:
        ticker = opp.get("vehicle", "")
        score = opp.get("total_score", 0)
        opp_id = opp["id"]
        is_asx = ticker.endswith(".AX")

        def skip(reason: str) -> None:
            insert_paper_skipped({
                "opportunity_id": opp_id,
                "ticker": ticker,
                "score": score,
                "skip_reason": reason,
                "week_of": week_of,
            })
            print(f"[paper/entry] SKIP {ticker} (score {score}) — {reason}")

        # 1. Score gate (regime-aware)
        bearish = bearish_asx if is_asx else bearish_us
        min_score = BEARISH_MIN_SCORE if bearish else MIN_SCORE
        if score < min_score:
            skip(f"score_{score}_below_{min_score}{'_bearish_regime' if bearish else ''}")
            continue

        # 2. Position cap
        if open_count >= MAX_POSITIONS:
            skip("max_positions_reached")
            break

        # 3. Duplicate ticker
        if ticker in open_tickers:
            skip("ticker_already_open")
            continue

        # 4. Already entered this opportunity
        if paper_position_exists_for_opportunity(opp_id):
            continue  # silent — normal on daily re-runs

        # 5. Recency
        ok, reason = _recency_ok(opp)
        if not ok:
            skip(reason)
            continue

        # 6. Price + earnings date (single API call)
        current_price, days_to_earnings = _fetch_price_and_earnings(ticker)
        if current_price is None:
            skip("price_fetch_failed")
            continue

        # 7. Price movement since scoring
        price_at_score = opp.get("price_at_score")
        if price_at_score and float(price_at_score) > 0:
            move_pct = abs(current_price - float(price_at_score)) / float(price_at_score) * 100
            if move_pct > MAX_PRICE_MOVE_PCT:
                skip(f"price_moved_{move_pct:.1f}pct")
                continue

        # 8. Earnings blackout
        if days_to_earnings is not None and 0 <= days_to_earnings <= EARNINGS_BLACKOUT_DAYS:
            skip(f"earnings_in_{days_to_earnings}d")
            continue

        # 9. Relative volume — threshold depends on pattern type
        pattern = opp.get("pattern", "")
        vol_threshold = (
            MIN_RELATIVE_VOLUME_EDGAR if pattern in EDGAR_PATTERNS
            else MIN_RELATIVE_VOLUME_NEWS
        )
        rel_vol = _fetch_relative_volume(ticker)
        if rel_vol is not None and rel_vol < vol_threshold:
            skip(f"low_relative_volume_{rel_vol:.2f}x")
            continue

        # Build position
        if is_asx:
            entry_price_aud = round(current_price * (1 + SLIPPAGE_PCT / 100), 4)
            entry_price_usd = round(entry_price_aud * fx_rate, 4)
            brokerage = ASX_BROKERAGE_AUD
            market = "ASX"
        else:
            entry_price_usd = round(current_price * (1 + SLIPPAGE_PCT / 100), 4)
            entry_price_aud = round(entry_price_usd / fx_rate, 4)
            brokerage = US_BROKERAGE_AUD
            market = "US"

        # 11. Minimum 1 share
        quantity = int(POSITION_SIZE_AUD / entry_price_aud)
        if quantity < 1:
            skip(f"price_too_high_aud:{entry_price_aud:.2f}")
            continue

        pos = {
            "opportunity_id": opp_id,
            "ticker": ticker,
            "pattern": opp.get("pattern", "unknown"),
            "market": market,
            "entry_price_usd": entry_price_usd,
            "entry_price_aud": entry_price_aud,
            "quantity": quantity,
            "brokerage_aud": brokerage,
            "entry_date": date.today().isoformat(),
            "entry_week_of": week_of,
            "score_at_entry": score,
            "status": "open",
        }

        insert_paper_position(pos)
        auto_fill_feedback_entry(opp_id, entry_price_aud)

        open_count += 1
        open_tickers.add(ticker)
        entered += 1

        cost_aud = entry_price_aud * quantity + brokerage
        regime_note = " [bearish regime]" if bearish else ""
        print(
            f"[paper/entry] ENTER {ticker} ({market}) @ "
            f"${entry_price_aud:.2f} AUD × {quantity} = ${cost_aud:.2f} AUD "
            f"(score {score}/20{regime_note})"
        )

    print(f"[paper/entry] done — {entered} entered, {open_count}/{MAX_POSITIONS} open")
